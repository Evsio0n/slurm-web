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

export type JobCheckState =
  | 'queued'
  | 'running'
  | 'passed'
  | 'failed'
  | 'warning'
  | 'skipped'
  | 'cancelled'
  | 'stalled'

export interface JobCheckEvent {
  schema: number
  seq: number
  timestamp: string
  job_id: number
  step_id: string
  task_id: number
  node: string
  check_id: string
  title: string
  state: JobCheckState
  message: string
  progress?: number
  current?: number
  total?: number
  duration_ms?: number
  metrics: Record<string, string | number>
  stale_seconds?: number
}

export interface JobChecksSnapshot {
  job_id: number
  cursor: number
  generated_at: number
  checks: JobCheckEvent[]
}

export type JobLiveChannel = 'checks' | 'log' | 'gpu' | 'job'

export interface JobLiveCursors {
  checks: number
  log: { stream: 'stdout' | 'stderr'; offset: number }
}

export interface JobExitSummary {
  status: string[]
  return_code: number | null
  signal: number | null
  signal_name: string
}

export interface JobLiveStepSummary {
  id: string | null
  name: string | null
  state: string[]
  elapsed: number
  start: number | null
  end: number | null
  exit_code: JobExitSummary | null
  nodes: string
  node_count: number
  tasks: number
}

export interface JobErrorExcerpt {
  stream: 'stdout' | 'stderr'
  path: string
  line: number
  total_lines: number
  matched: boolean
  lines: string[]
  truncated: boolean
}

export interface JobDiagnostics {
  states: string[]
  reason: string
  exit_code: JobExitSummary | null
  derived_exit_code: JobExitSummary | null
  failed_steps: { id: string | null; name: string | null; state: string[]; exit_code: JobExitSummary | null; nodes: string }[]
  excerpt: JobErrorExcerpt | null
}

export interface JobLiveJobSummary {
  state: string[]
  reason: string
  elapsed: number
  start: number | null
  end: number | null
  nodes: string
  exit_code: JobExitSummary | null
  derived_exit_code: JobExitSummary | null
  steps: JobLiveStepSummary[]
  active: boolean
  diagnostics: JobDiagnostics | null
}

export type JobLiveServerMessage =
  | { type: 'ready'; job_id: number; channels: JobLiveChannel[] }
  | ({ type: 'subscribed'; channels: JobLiveChannel[] } & { cursors: Record<string, unknown> })
  | ({ type: 'checks' } & JobChecksSnapshot)
  | { type: 'check'; event: JobCheckEvent }
  | ({ type: 'log'; stream: 'stdout' | 'stderr'; merged?: boolean } & JobLogChunk)
  | ({ type: 'gpu' } & JobGpuTelemetry)
  | ({ type: 'job' } & JobLiveJobSummary)
  | { type: 'heartbeat'; ts: number; cursor: number }
  | { type: 'pong'; ts: number }
  | { type: 'error'; code: number; message: string; transient?: boolean }
