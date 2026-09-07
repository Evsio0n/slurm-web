export interface JobLogChunk {
  path: string
  chunk: string
  offset: number
  next_offset: number
  size?: number
  rotated?: boolean
  waiting: boolean
}

export interface JobGpuProcess {
  pid: number
  name: string
  memory_used_mb: number
  job_id: string
}

export interface JobGpuDevice {
  index: number
  uuid: string
  name: string
  utilization_gpu: number
  utilization_memory: number
  memory_used_mb: number
  memory_total_mb: number
  temperature: number
  power_watts: number
  power_limit_watts: number
  job_ids: string[]
  processes: JobGpuProcess[]
}

export interface JobGpuSnapshot {
  node: string
  timestamp: number
  age_seconds: number
  stale: boolean
  gpus: JobGpuDevice[]
}

export interface JobGpuTelemetry {
  nodes: JobGpuSnapshot[]
  summary: {
    count: number
    utilization: number
    memory_used_mb: number
    memory_total_mb: number
    power_watts: number
    temperature_max: number
  }
}
