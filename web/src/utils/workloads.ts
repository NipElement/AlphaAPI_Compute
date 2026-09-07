export type WorkloadKind = "devmachines" | "jobs" | "services" | "volumes";

export const WORKLOAD_META = {
  devmachines: {
    nav: "devMachines",
    desc: "devDesc",
    create: "createDev",
    empty: "noDev",
    icon: "icon-desktop",
    example: "my-workspace",
  },
  jobs: {
    nav: "jobs",
    desc: "jobsDesc",
    create: "createJob",
    empty: "noJobs",
    icon: "icon-thunderbolt",
    example: "training-run",
  },
  services: {
    nav: "services",
    desc: "servicesDesc",
    create: "createService",
    empty: "noServices",
    icon: "icon-cloud",
    example: "my-service",
  },
  volumes: {
    nav: "volumes",
    desc: "volumesDesc",
    create: "createVolume",
    empty: "noVolumes",
    icon: "icon-storage",
    example: "project-data",
  },
} as const;
