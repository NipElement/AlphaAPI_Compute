// fake-gpu-plugin — kubelet device plugin (v1beta1) for arise.dev/fake-gpu.
// Closes the two OPEN rows of plan §8.2 (see runbooks/gaps.md §1):
//   - device IDs handed to containers (ARISE_FAKE_GPU_IDS via Allocate)
//   - kubelet-driven allocatable updates (health flows through ListAndWatch)
//
// Dependency policy: exactly two direct deps — the gRPC runtime and the
// upstream kubelet device-plugin API types. Everything else is transitive.
// Versions resolve through proxy.golang.org at image build; the builder image
// is pinned by digest in versions.env (GO_BUILDER_IMAGE) and the resolved
// module list is recorded into the build log kept under evidence/.
module arise.dev/fake-gpu-plugin

go 1.23

require (
	google.golang.org/grpc v1.65.0
	k8s.io/kubelet v0.31.1
)

require (
	github.com/gogo/protobuf v1.3.2 // indirect
	golang.org/x/net v0.26.0 // indirect
	golang.org/x/sys v0.21.0 // indirect
	golang.org/x/text v0.16.0 // indirect
	google.golang.org/genproto/googleapis/rpc v0.0.0-20240701130421-f6361c86f094 // indirect
	google.golang.org/protobuf v1.34.2 // indirect
)
