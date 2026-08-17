// fake-gpu-plugin — a real kubelet device plugin (device plugin API v1beta1)
// serving the simulated resource `arise.dev/fake-gpu`.
//
// ============================================================================
// HONEST SCOPE STATEMENT (plan §8.1: SIMULATED / CONTROL-PLANE / HARDWARE)
// ============================================================================
// This component is CONTROL-PLANE. The *devices* are simulated (no hardware is
// touched), but the *mechanism* is the genuine kubelet device-plugin contract:
//
//   - registration over the kubelet Registration socket, plugin socket
//     lifecycle, re-registration after kubelet restart;
//   - ListAndWatch: kubelet's device manager — not any of our controllers —
//     computes node capacity/allocatable from the reported device health
//     (closes gaps.md §1 row "健康行为 … 随 kubelet 更新");
//   - Allocate: containers requesting the resource receive the env var
//     ARISE_FAKE_GPU_IDS with the exact device IDs the kubelet assigned
//     (closes gaps.md §1 row "分配行为返回 ARISE_FAKE_GPU_IDS").
//
// What remains simulated and MUST NOT be claimed: the devices themselves
// (no /dev nodes, no NUMA topology, no real health probing). Real GPU serving
// on DGX hardware uses NVIDIA's device plugin via the GPU Operator (dgx
// overlay); this plugin exists so Phase A exercises the same kubelet contract
// the hardware path relies on. It runs ONLY in overlays/lab.
//
// Fault injection: the fake-gpu-advertiser keeps the public test API
// (/test/unhealthy, /test/reset — plan §11.1) and forwards to the admin
// endpoint this plugin serves on the pod IP. The plugin never talks to the
// Kubernetes API server: its ServiceAccount token is not even mounted.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	pluginapi "k8s.io/kubelet/pkg/apis/deviceplugin/v1beta1"
)

const (
	defaultResource = "arise.dev/fake-gpu"
	socketBase      = "arise-fake-gpu.sock"
	envIDs          = "ARISE_FAKE_GPU_IDS"
	envCount        = "ARISE_FAKE_GPU_COUNT"
)

// ----------------------------------------------------------------- logging --
// Same JSON shape as the Python controllers (plan §9.5 machine-parsable logs).
func logj(level, msg string, kv ...any) {
	rec := map[string]any{
		"timestamp":  time.Now().UTC().Format("2006-01-02T15:04:05Z"),
		"level":      level,
		"controller": "fake-gpu-plugin",
		"msg":        msg,
	}
	for i := 0; i+1 < len(kv); i += 2 {
		rec[fmt.Sprint(kv[i])] = kv[i+1]
	}
	b, _ := json.Marshal(rec)
	fmt.Println(string(b))
}

// ------------------------------------------------------------------ plugin --
type plugin struct {
	resource string
	logical  string // dgx01..dgx04 — the stable, globally-unique ID prefix
	perNode  int
	sockPath string

	mu        sync.Mutex
	unhealthy map[string]string // device ID -> faultId

	update chan struct{} // kicks ListAndWatch to resend
	stopCh chan struct{}
	srv    *grpc.Server
}

func newPlugin(resource, logical string, perNode int) *plugin {
	return &plugin{
		resource:  resource,
		logical:   logical,
		perNode:   perNode,
		sockPath:  filepath.Join(pluginapi.DevicePluginPath, socketBase),
		unhealthy: map[string]string{},
		update:    make(chan struct{}, 1),
		stopCh:    make(chan struct{}),
	}
}

func (p *plugin) deviceIDs() []string {
	ids := make([]string, 0, p.perNode)
	for i := 0; i < p.perNode; i++ {
		ids = append(ids, fmt.Sprintf("%s-fake-gpu-%d", p.logical, i))
	}
	return ids
}

func (p *plugin) devices() []*pluginapi.Device {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make([]*pluginapi.Device, 0, p.perNode)
	for _, id := range p.deviceIDs() {
		health := pluginapi.Healthy
		if _, bad := p.unhealthy[id]; bad {
			health = pluginapi.Unhealthy
		}
		out = append(out, &pluginapi.Device{ID: id, Health: health})
	}
	return out
}

func (p *plugin) kick() {
	select {
	case p.update <- struct{}{}:
	default: // an update is already queued; ListAndWatch reads fresh state
	}
}

// ------------------------------------------------ device plugin gRPC API --
func (p *plugin) GetDevicePluginOptions(context.Context, *pluginapi.Empty) (*pluginapi.DevicePluginOptions, error) {
	// No PreStartContainer, no GetPreferredAllocation preference.
	return &pluginapi.DevicePluginOptions{}, nil
}

func (p *plugin) ListAndWatch(_ *pluginapi.Empty, s pluginapi.DevicePlugin_ListAndWatchServer) error {
	// First response is the full inventory; kubelet's device manager derives
	// capacity (all devices) and allocatable (healthy devices) from it. Every
	// health transition resends the list — the node status update is then
	// kubelet's own doing, which is exactly the §8.2 row this closes.
	if err := s.Send(&pluginapi.ListAndWatchResponse{Devices: p.devices()}); err != nil {
		return err
	}
	for {
		select {
		case <-s.Context().Done():
			// kubelet closed this stream (re-registration) or srv.Stop() ran.
			// Without this case the handler blocks forever on p.update and,
			// after a kubelet restart, the leaked old handler races the live
			// one for each health token on the shared size-1 channel — it can
			// swallow the very allocatable transition SCH-07/SCH-11 assert.
			return nil
		case <-p.stopCh:
			return nil
		case <-p.update:
			if err := s.Send(&pluginapi.ListAndWatchResponse{Devices: p.devices()}); err != nil {
				return err
			}
		}
	}
}

func (p *plugin) Allocate(_ context.Context, req *pluginapi.AllocateRequest) (*pluginapi.AllocateResponse, error) {
	resp := &pluginapi.AllocateResponse{}
	for _, creq := range req.GetContainerRequests() {
		ids := append([]string(nil), creq.GetDevicesIDs()...)
		sort.Strings(ids) // deterministic env content for evidence diffing
		logj("INFO", "allocate", "devices", ids)
		resp.ContainerResponses = append(resp.ContainerResponses,
			&pluginapi.ContainerAllocateResponse{
				Envs: map[string]string{
					envIDs:   strings.Join(ids, ","),
					envCount: strconv.Itoa(len(ids)),
				},
			})
	}
	return resp, nil
}

func (p *plugin) GetPreferredAllocation(context.Context, *pluginapi.PreferredAllocationRequest) (*pluginapi.PreferredAllocationResponse, error) {
	return &pluginapi.PreferredAllocationResponse{}, nil
}

func (p *plugin) PreStartContainer(context.Context, *pluginapi.PreStartContainerRequest) (*pluginapi.PreStartContainerResponse, error) {
	return &pluginapi.PreStartContainerResponse{}, nil
}

// -------------------------------------------------- serve + registration --
func (p *plugin) serve() error {
	_ = os.Remove(p.sockPath)
	lis, err := net.Listen("unix", p.sockPath)
	if err != nil {
		return fmt.Errorf("listen %s: %w", p.sockPath, err)
	}
	p.srv = grpc.NewServer()
	pluginapi.RegisterDevicePluginServer(p.srv, p)
	go func() {
		if err := p.srv.Serve(lis); err != nil {
			logj("ERROR", "grpc serve ended", "detail", err.Error())
		}
	}()
	// Wait until our own socket answers before telling kubelet about it.
	conn, err := dial(p.sockPath, 5*time.Second)
	if err != nil {
		return fmt.Errorf("self-dial: %w", err)
	}
	_ = conn.Close()
	return nil
}

func (p *plugin) register() error {
	conn, err := dial(pluginapi.KubeletSocket, 5*time.Second)
	if err != nil {
		return fmt.Errorf("dial kubelet: %w", err)
	}
	defer conn.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_, err = pluginapi.NewRegistrationClient(conn).Register(ctx, &pluginapi.RegisterRequest{
		Version:      pluginapi.Version,
		Endpoint:     socketBase,
		ResourceName: p.resource,
	})
	return err
}

func dial(sock string, timeout time.Duration) (*grpc.ClientConn, error) {
	conn, err := grpc.NewClient("unix://"+sock,
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, err
	}
	// NewClient connects lazily; force a bounded readiness probe.
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	conn.Connect()
	for {
		st := conn.GetState()
		if st.String() == "READY" {
			return conn, nil
		}
		if !conn.WaitForStateChange(ctx, st) {
			_ = conn.Close()
			return nil, fmt.Errorf("socket %s not ready within %s", sock, timeout)
		}
	}
}

// --------------------------------------------------------- admin HTTP API --
// Listens on ALL interfaces (0.0.0.0:PORT) — it has to: the kubelet
// readiness/liveness probes reach it from the node's host network, and the
// fake-gpu-advertiser reaches it from the pod network. Binding a single
// interface would break one of those. The endpoint mutates simulated device
// health, so it is UNAUTHENTICATED at the HTTP layer and fenced at the network
// layer instead: NetworkPolicy `platform-internal-ingress` (platform/base/
// networkpolicies.yaml) denies ingress from the tenant namespaces, so only the
// platform trust zone (advertiser, monitoring, kubelet probes) can reach it.
// Do NOT rely on this being unreachable without that policy in place.
// Endpoint shapes mirror the advertiser's own so the SCH-07 surface stays
// byte-compatible upstream.
func (p *plugin) adminMux() *http.ServeMux {
	mux := http.NewServeMux()
	writeJSON := func(w http.ResponseWriter, code int, v any) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(code)
		_ = json.NewEncoder(w).Encode(v)
	}
	owns := func(id string) bool {
		return strings.HasPrefix(id, p.logical+"-fake-gpu-")
	}
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, 200, map[string]string{"status": "ok", "node": p.logical})
	})
	mux.HandleFunc("GET /devices", func(w http.ResponseWriter, _ *http.Request) {
		p.mu.Lock()
		dead := make([]string, 0, len(p.unhealthy))
		for id := range p.unhealthy {
			dead = append(dead, id)
		}
		p.mu.Unlock()
		sort.Strings(dead)
		writeJSON(w, 200, map[string]any{
			"resource": p.resource, "node": p.logical,
			"devices": p.deviceIDs(), "unhealthy": dead,
		})
	})
	setHealth := func(healthy bool) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			var body struct {
				Device  string `json:"device"`
				FaultID string `json:"faultId"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Device == "" {
				writeJSON(w, 400, map[string]string{"error": "device required"})
				return
			}
			if !owns(body.Device) {
				writeJSON(w, 404, map[string]string{"error": "device not on this node",
					"node": p.logical})
				return
			}
			p.mu.Lock()
			if healthy {
				delete(p.unhealthy, body.Device)
			} else {
				p.unhealthy[body.Device] = body.FaultID
			}
			p.mu.Unlock()
			p.kick()
			lvl, verb := "INFO", "device restored"
			if !healthy {
				lvl, verb = "WARN", "device marked unhealthy"
			}
			logj(lvl, verb, "device", body.Device, "fault_id", body.FaultID)
			writeJSON(w, 200, map[string]any{"device": body.Device, "healthy": healthy})
		}
	}
	mux.HandleFunc("POST /unhealthy", setHealth(false))
	mux.HandleFunc("POST /healthy", setHealth(true))
	mux.HandleFunc("POST /reset", func(w http.ResponseWriter, _ *http.Request) {
		p.mu.Lock()
		p.unhealthy = map[string]string{}
		p.mu.Unlock()
		p.kick()
		logj("INFO", "all devices restored")
		writeJSON(w, 200, map[string]bool{"reset": true})
	})
	return mux
}

// -------------------------------------------------------------------- main --
func main() {
	nodeName := os.Getenv("NODE_NAME")
	nodeMapJSON := os.Getenv("NODE_MAP_JSON")
	resource := envOr("FAKE_GPU_RESOURCE", defaultResource)
	perNode := envInt("FAKE_GPU_PER_NODE", 8)
	port := envInt("PORT", 8080)

	nodeMap := map[string]string{}
	if err := json.Unmarshal([]byte(nodeMapJSON), &nodeMap); err != nil || len(nodeMap) == 0 {
		logj("ERROR", "NODE_MAP_JSON missing or invalid; refusing to start")
		os.Exit(1)
	}
	logical, ok := nodeMap[nodeName]
	if !ok {
		// DaemonSet nodeSelector should make this unreachable; if the node-map
		// and the selector ever disagree, failing loud beats advertising
		// devices with an unstable identity (§8.2 "稳定且全局唯一").
		logj("ERROR", "node not in NODE_MAP_JSON", "node", nodeName)
		os.Exit(1)
	}

	p := newPlugin(resource, logical, perNode)
	logj("INFO", "starting", "node", nodeName, "logical", logical,
		"resource", resource, "per_node", perNode, "socket", p.sockPath)

	go func() {
		addr := fmt.Sprintf(":%d", port)
		if err := http.ListenAndServe(addr, p.adminMux()); err != nil {
			logj("ERROR", "admin http ended", "detail", err.Error())
			os.Exit(1)
		}
	}()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGTERM, syscall.SIGINT)

	// Serve + register, then watch for the kubelet wiping the plugin dir
	// (its restart signal): our socket vanishing means we must re-register.
	// This poll-based watcher is the documented pattern; fsnotify would add a
	// dependency for no behavioural gain at a 4 s granularity.
	for {
		if err := p.serve(); err != nil {
			logj("ERROR", "serve failed; retrying", "detail", err.Error())
			time.Sleep(3 * time.Second)
			continue
		}
		if err := p.register(); err != nil {
			logj("ERROR", "register failed; retrying", "detail", err.Error())
			p.srv.Stop()
			time.Sleep(3 * time.Second)
			continue
		}
		logj("INFO", "registered with kubelet", "endpoint", socketBase)

	watch:
		for {
			select {
			case <-sig:
				logj("INFO", "terminating")
				p.srv.Stop()
				_ = os.Remove(p.sockPath)
				os.Exit(0)
			case <-time.After(4 * time.Second):
				if _, err := os.Lstat(p.sockPath); err != nil {
					logj("WARN", "plugin socket removed (kubelet restart?); re-registering")
					p.srv.Stop()
					break watch
				}
			}
		}
	}
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func envInt(k string, d int) int {
	if v := os.Getenv(k); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return d
}
