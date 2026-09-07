<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import type { SlurmJobDetail, SlurmJobStep } from '@/composables/gateway/slurm/types'
import type {
  JobCheckEvent,
  JobDiagnostics,
  JobExitSummary,
  JobGpuTelemetry,
  JobLiveJobSummary,
  JobLiveServerMessage,
  JobLogChunk
} from '@/composables/gateway/types/engineer'
import { useGatewayAPI } from '@/composables/GatewayAPI'
import { useJobLiveSocket } from '@/composables/useJobLiveSocket'
import { LogModel } from '@/composables/logParser'
import { extractSlurmTRESResources } from '@/composables/gateway/slurm/tres'
import { jobAllocatedGPU } from '@/composables/gateway/slurm/job'
import { fetchEventSource } from '@microsoft/fetch-event-source'
import { useHttp } from '@/plugins/http'
import { useAuthStore } from '@/stores/auth'
import JobLogViewer from '@/components/job/JobLogViewer.vue'
import type { LogStreamTab } from '@/components/job/JobLogViewer.vue'

type Stream = 'stdout' | 'stderr'
const STREAMS: Stream[] = ['stdout', 'stderr']
const CHECK_SNIPPET = [
  'slurm-check section start load-model "Load model"',
  'slurm-check running load-model --progress 40 --message "shard 12/29"',
  'slurm-check passed load-model --duration-ms 31042',
  'slurm-check section end load-model'
].join('\n')
const TERMINAL_FAILURE = ['FAILED', 'OUT_OF_MEMORY', 'TIMEOUT', 'NODE_FAIL', 'CANCELLED', 'BOOT_FAIL', 'DEADLINE', 'PREEMPTED']

const props = defineProps<{ cluster: string; id: number; job: SlurmJobDetail }>()
const gateway = useGatewayAPI()
const http = useHttp()
const auth = useAuthStore()

/* Output streams: one parsed model and one byte cursor per stream. */
const models = { stdout: new LogModel(), stderr: new LogModel() }
const logVersion = ref(0)
const offsets = ref<Record<Stream, number>>({ stdout: 0, stderr: 0 })
const paths = ref<Record<Stream, string>>({ stdout: '', stderr: '' })
const merged = ref(false)
const stream = ref<Stream>('stdout')
const paused = ref(false)
const logError = ref('')
const viewer = ref<InstanceType<typeof JobLogViewer>>()
const activeModel = shallowRef(models.stdout)

const gpu = ref<JobGpuTelemetry>()
const checks = ref<JobCheckEvent[]>([])
const checksCursor = ref(0)
const checksClock = ref(Date.now())
const liveJob = ref<JobLiveJobSummary>()
const diagnostics = ref<JobDiagnostics | null>(null)
let checksClockTimer = -1

/* Transport: WebSocket first, REST polling + SSE only when it never worked. */
const fallback = ref(false)
let fallbackChecksController = new AbortController()
let logTimer = -1
let gpuTimer = -1

const live = useJobLiveSocket({
  cluster: props.cluster,
  jobId: () => props.id,
  /* Both streams are subscribed so the tabs carry counts and switching is instant. */
  cursors: () => ({ checks: checksCursor.value, log: { stdout: offsets.value.stdout, stderr: offsets.value.stderr } }),
  onMessage: handleLiveMessage,
  onFallback: startFallback
})

const transport = computed(() => {
  if (fallback.value) return { label: 'SSE · POLLING', state: 'fallback' }
  if (live.state.value === 'live') return { label: 'WS · LIVE', state: 'live' }
  if (live.state.value === 'reconnecting') return { label: 'WS · RECONNECTING', state: 'reconnecting' }
  if (live.state.value === 'closed') return { label: 'WS · CLOSED', state: 'reconnecting' }
  return { label: 'WS · CONNECTING', state: 'connecting' }
})

/* Derived job facts. The live summary wins over the slower detail poller. */
const resources = computed(() => extractSlurmTRESResources(props.job.tres.allocated))
const allocatedGpu = computed(() => Math.max(0, jobAllocatedGPU(props.job)))
const elapsed = computed(() => liveJob.value?.elapsed ?? props.job.time.elapsed ?? 0)
const status = computed(() => liveJob.value?.state[0] || props.job.state.current[0] || 'UNKNOWN')
const isActive = computed(() => (liveJob.value ? liveJob.value.active : !['COMPLETED', ...TERMINAL_FAILURE].includes(status.value)))
const limitSeconds = computed(() =>
  props.job.time.limit?.set && !props.job.time.limit.infinite ? props.job.time.limit.number * 60 : 0
)
const progress = computed(() => (limitSeconds.value ? Math.min(100, (elapsed.value / limitSeconds.value) * 100) : 0))
const gpuHours = computed(() => (allocatedGpu.value * elapsed.value) / 3600)
const cpuHours = computed(() => (Math.max(0, resources.value.cpu) * elapsed.value) / 3600)
const gpuBudgetHours = computed(() => (limitSeconds.value ? (allocatedGpu.value * limitSeconds.value) / 3600 : 0))
const allGpus = computed(() => gpu.value?.nodes.flatMap((node) => node.gpus.map((device) => ({ node, device }))) || [])
const checkGroups = computed(() => {
  const groups = new Map<string, { step: string; task: number; node: string; checks: JobCheckEvent[] }>()
  for (const check of checks.value) {
    const key = `${check.step_id}:${check.task_id}`
    if (!groups.has(key)) groups.set(key, { step: check.step_id, task: check.task_id, node: check.node, checks: [] })
    groups.get(key)?.checks.push(check)
  }
  return [...groups.values()]
})

interface Stage {
  id: string
  name: string
  state: string
  elapsed: number
  share: number
  exit?: JobExitSummary | null
  nodes?: string
  tasks?: number
  peak: string
  section: boolean
}

const stages = computed<Stage[]>(() => {
  const source = liveJob.value?.steps.length
    ? liveJob.value.steps.map((step) => ({
        id: step.id || 'batch',
        name: step.name || step.id || 'batch',
        state: step.state[0] || 'UNKNOWN',
        elapsed: step.elapsed,
        exit: step.exit_code,
        nodes: step.nodes,
        tasks: step.tasks,
        peak: ''
      }))
    : props.job.steps.map((step) => ({
        id: step.step.id,
        name: stepName(step),
        state: stepStatus(step),
        elapsed: step.time.elapsed,
        exit: exitSummary(step),
        nodes: step.nodes?.range,
        tasks: step.tasks?.count,
        peak: stepPeakMemory(step)
      }))
  const total = Math.max(1, ...source.map((s) => s.elapsed))
  void logVersion.value
  const sections = new Set(models.stdout.sections.map((s) => s.name))
  return source.map((s) => ({ ...s, share: (s.elapsed / total) * 100, section: sections.has(s.name) || sections.has(s.id) }))
})

const streamTabs = computed<LogStreamTab[]>(() => {
  void logVersion.value
  return STREAMS.map((id) => ({
    id,
    lines: models[id].lines.length,
    errors: models[id].errorLines,
    merged: id === 'stderr' && merged.value
  }))
})

const failure = computed(() => {
  const d = diagnostics.value
  if (!d) return undefined
  const exit = d.exit_code
  const derived = d.derived_exit_code
  const step = d.failed_steps[0]
  return {
    title: d.states[0] || status.value,
    exit: exitLabel(exit),
    derived: derived && (derived.return_code || derived.signal) ? exitLabel(derived) : '',
    reason: d.reason && d.reason !== 'None' ? d.reason : '',
    step: step ? `${step.name || step.id}${step.exit_code ? ` · ${exitLabel(step.exit_code)}` : ''}${step.nodes ? ` · ${step.nodes}` : ''}` : '',
    steps: d.failed_steps.length,
    excerpt: d.excerpt
  }
})

function exitSummary(step: SlurmJobStep): JobExitSummary | null {
  const code = step.exit_code
  if (!code) return null
  return {
    status: code.status,
    return_code: code.return_code?.set ? code.return_code.number : null,
    signal: code.signal?.id?.set ? code.signal.id.number : null,
    signal_name: code.signal?.name || ''
  }
}

function exitLabel(exit: JobExitSummary | null | undefined) {
  if (!exit) return ''
  if (exit.signal) return `signal ${exit.signal}${exit.signal_name ? ` (${exit.signal_name})` : ''}`
  if (exit.return_code === null) return exit.status[0] || ''
  return `exit ${exit.return_code}`
}

function duration(value: number) {
  const seconds = Math.max(0, Math.floor(value))
  const d = Math.floor(seconds / 86400)
  const h = Math.floor((seconds % 86400) / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = seconds % 60
  return `${d ? `${d}d ` : ''}${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

function number(value: number, digits = 1) {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: digits }).format(value || 0)
}

function stepStatus(step: SlurmJobStep): string {
  return step.state?.[0] || 'UNKNOWN'
}

function stepName(step: SlurmJobStep): string {
  return step.step?.name || step.step?.id || 'batch'
}

function stepPeakMemory(step: SlurmJobStep): string {
  const entry = step.tres?.requested?.max?.find((item) => item.type === 'mem')
  if (!entry?.count) return ''
  return `${number(entry.count / 1024 ** 3, 1)} GiB peak`
}

function checkIcon(check: JobCheckEvent) {
  const state = effectiveCheckState(check)
  if (state === 'passed') return '✓'
  if (state === 'failed' || state === 'cancelled') return '×'
  if (state === 'warning' || state === 'stalled') return '!'
  if (state === 'skipped') return '–'
  return '●'
}

function effectiveCheckState(check: JobCheckEvent) {
  if (check.state !== 'running') return check.state
  const age = checksClock.value - Date.parse(check.timestamp)
  return Number.isFinite(age) && age > 15_000 ? 'stalled' : 'running'
}

function checkProgress(check: JobCheckEvent) {
  if (check.progress !== undefined && check.progress !== null) return check.progress
  if (check.current !== undefined && check.total) return (check.current / check.total) * 100
  return undefined
}

function upsertCheck(event: JobCheckEvent) {
  const index = checks.value.findIndex(
    (item) => item.step_id === event.step_id && item.task_id === event.task_id && item.check_id === event.check_id
  )
  if (index === -1) checks.value.push(event)
  else checks.value[index] = event
  checks.value.sort((a, b) => a.seq - b.seq)
  checksCursor.value = Math.max(checksCursor.value, event.seq)
}

function appendLog(id: Stream, chunk: JobLogChunk & { merged?: boolean }) {
  if (chunk.merged) {
    merged.value = true
    paths.value.stderr = chunk.path
    return
  }
  if (chunk.path) paths.value[id] = chunk.path
  logError.value = ''
  if (chunk.rotated) {
    models[id].reset()
    offsets.value[id] = 0
  }
  if (chunk.chunk) models[id].append(chunk.chunk)
  offsets.value[id] = chunk.next_offset
  logVersion.value++
}

function handleLiveMessage(message: JobLiveServerMessage) {
  switch (message.type) {
    case 'checks':
      checks.value = message.checks
      checksCursor.value = Math.max(checksCursor.value, message.cursor)
      break
    case 'check':
      upsertCheck(message.event)
      break
    case 'log':
      appendLog(message.stream, message)
      break
    case 'gpu':
      gpu.value = { nodes: message.nodes, summary: message.summary }
      break
    case 'job':
      liveJob.value = message
      if (message.diagnostics !== undefined) diagnostics.value = message.diagnostics
      if (!message.active) {
        models.stdout.flush()
        models.stderr.flush()
        logVersion.value++
      }
      break
    case 'error':
      if (!message.transient) logError.value = message.message
      break
    default:
      break
  }
}

/* Fallback transport: previous REST polling and SSE, only when WebSocket gave up. */

async function loadChecks() {
  const snapshot = await gateway.jobChecks(props.cluster, props.id)
  checks.value = snapshot.checks
  checksCursor.value = snapshot.cursor
}

async function startFallbackChecks() {
  fallbackChecksController.abort()
  fallbackChecksController = new AbortController()
  try {
    await loadChecks()
    const base = http.defaults.baseURL || '/api/'
    await fetchEventSource(
      `${base}agents/${encodeURIComponent(props.cluster)}/job/${props.id}/checks/events?cursor=${checksCursor.value}`,
      {
        headers: { Authorization: `Bearer ${auth.token}` },
        signal: fallbackChecksController.signal,
        openWhenHidden: true,
        onopen: async (response) => {
          if (!response.ok) throw new Error(`Checks stream HTTP ${response.status}`)
        },
        onmessage: (message) => {
          if (message.event === 'check' && message.data) upsertCheck(JSON.parse(message.data) as JobCheckEvent)
        },
        onclose: () => {
          throw new Error('Checks stream closed')
        },
        onerror: () => 1000
      }
    )
  } catch {
    /* fetchEventSource retries on its own until aborted */
  }
}

async function pollLog() {
  if (paused.value) return
  try {
    const chunk = await gateway.jobLog(props.cluster, props.id, stream.value, offsets.value[stream.value])
    appendLog(stream.value, chunk)
  } catch (error) {
    logError.value = error instanceof Error ? error.message : String(error)
  }
}

async function pollGpu() {
  try {
    gpu.value = await gateway.jobGpus(props.cluster, props.id)
  } catch {
    gpu.value = undefined
  }
}

async function pollDiagnostics() {
  if (isActive.value) return
  try {
    diagnostics.value = await gateway.jobDiagnostics(props.cluster, props.id)
  } catch {
    diagnostics.value = null
  }
}

function startFallback() {
  if (fallback.value) return
  fallback.value = true
  void pollLog()
  void pollGpu()
  void pollDiagnostics()
  void startFallbackChecks()
  logTimer = window.setInterval(pollLog, 1000)
  gpuTimer = window.setInterval(pollGpu, 2000)
}

function stopFallback() {
  window.clearInterval(logTimer)
  window.clearInterval(gpuTimer)
  fallbackChecksController.abort()
}

/* User controls */

function togglePause() {
  paused.value = !paused.value
  if (!fallback.value) live.send({ type: paused.value ? 'pause' : 'resume', channel: 'log' })
}

function clearView() {
  /* Only the parsed view is cleared; byte offsets are kept so nothing is re-downloaded. */
  models[stream.value].reset()
  logVersion.value++
}

async function downloadLog() {
  try {
    const blob = await gateway.jobLogRaw(props.cluster, props.id, stream.value)
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `job-${props.id}-${stream.value}.log`
    link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 5000)
  } catch (error) {
    logError.value = error instanceof Error ? error.message : String(error)
  }
}

function openExcerpt() {
  const excerpt = diagnostics.value?.excerpt
  if (!excerpt) return
  if (excerpt.stream !== stream.value && !(excerpt.stream === 'stderr' && merged.value)) stream.value = excerpt.stream
  window.setTimeout(() => viewer.value?.scrollToLine(excerpt.line), 50)
}

function openStage(stage: Stage) {
  if (!stage.section) return
  if (stream.value !== 'stdout') stream.value = 'stdout'
  window.setTimeout(() => viewer.value?.scrollToSection(stage.name) || viewer.value?.scrollToSection(stage.id), 50)
}

function resetForNewJob() {
  models.stdout.reset()
  models.stderr.reset()
  offsets.value = { stdout: 0, stderr: 0 }
  paths.value = { stdout: '', stderr: '' }
  merged.value = false
  logVersion.value++
  checks.value = []
  checksCursor.value = 0
  gpu.value = undefined
  liveJob.value = undefined
  diagnostics.value = null
  if (fallback.value) {
    void pollLog()
    void pollGpu()
    void pollDiagnostics()
    void startFallbackChecks()
  } else {
    live.restart()
  }
}

watch(stream, (value) => {
  activeModel.value = models[value]
  if (fallback.value) void pollLog()
})
watch(() => props.id, resetForNewJob)
watch(isActive, (active, was) => {
  if (was && !active && fallback.value) void pollDiagnostics()
})

onMounted(() => {
  live.connect()
  checksClockTimer = window.setInterval(() => (checksClock.value = Date.now()), 1000)
})

onUnmounted(() => {
  window.clearInterval(checksClockTimer)
  stopFallback()
  live.close()
  gateway.abort()
})
</script>

<template>
  <div class="ch-workbench">
    <div class="ch-run-header">
      <div>
        <p class="ch-eyebrow">PIPELINE RUN · #{{ id }}</p>
        <h1>{{ job.name }}</h1>
        <p class="ch-subtitle">{{ job.user }} · {{ job.partition }} · {{ liveJob?.nodes || job.nodes || 'Waiting for allocation' }}</p>
      </div>
      <div class="ch-run-status">
        <span class="ch-status" :data-state="status">{{ status }}</span>
        <span class="ch-transport" :data-state="transport.state" :title="live.lastError.value || ''">{{ transport.label }}</span>
      </div>
    </div>

    <div class="ch-metrics">
      <div class="ch-metric"><span>Elapsed</span><strong>{{ duration(elapsed) }}</strong><small v-if="limitSeconds">{{ number(progress) }}% of {{ duration(limitSeconds) }}</small></div>
      <div class="ch-metric">
        <span>GPU consumption</span>
        <strong>{{ allocatedGpu ? `${number(gpuHours, 2)} GPU-h` : '—' }}</strong>
        <small>{{ allocatedGpu ? `${allocatedGpu} GPU allocated${gpuBudgetHours ? ` · ${number(gpuBudgetHours, 1)} GPU-h budget` : ''}` : 'no GPU allocated' }}</small>
      </div>
      <div class="ch-metric"><span>CPU consumption</span><strong>{{ number(cpuHours, 2) }} CPU-h</strong><small>{{ Math.max(0, resources.cpu) }} CPU allocated</small></div>
      <div class="ch-metric"><span>Live GPU util</span><strong>{{ isActive ? `${number(gpu?.summary.utilization || 0)}%` : '—' }}</strong><small>{{ isActive ? `${gpu?.summary.count || 0} GPU reporting` : 'job finished' }}</small></div>
      <div class="ch-metric"><span>GPU memory</span><strong>{{ isActive ? `${number((gpu?.summary.memory_used_mb || 0) / 1024)} GiB` : '—' }}</strong><small>{{ isActive ? `of ${number((gpu?.summary.memory_total_mb || 0) / 1024)} GiB` : 'job finished' }}</small></div>
      <div class="ch-metric"><span>Power</span><strong>{{ isActive ? `${number(gpu?.summary.power_watts || 0)} W` : '—' }}</strong><small>{{ isActive ? `max ${number(gpu?.summary.temperature_max || 0)} °C` : 'job finished' }}</small></div>
    </div>

    <section v-if="failure" class="ch-failure">
      <header>
        <div>× {{ failure.title }}<span v-if="failure.step"> · {{ failure.step }}</span></div>
        <span>{{ failure.exit }}<template v-if="failure.derived"> · derived {{ failure.derived }}</template></span>
      </header>
      <div class="ch-failure-facts">
        <div v-if="failure.reason">Reason <b>{{ failure.reason }}</b></div>
        <div v-if="failure.steps > 1">Failed steps <b>{{ failure.steps }}</b></div>
        <div v-if="failure.excerpt">Source <b>{{ failure.excerpt.stream }}:{{ failure.excerpt.line }}</b><template v-if="!failure.excerpt.matched"> (last lines, no error pattern matched)</template></div>
        <div v-else>No output file available for an error excerpt.</div>
      </div>
      <pre v-if="failure.excerpt">{{ failure.excerpt.lines.join('\n') }}{{ failure.excerpt.truncated ? '\n…' : '' }}</pre>
      <div v-if="failure.excerpt" class="ch-failure-actions">
        <button type="button" @click="openExcerpt">Open in log ↓</button>
      </div>
    </section>

    <section class="ch-panel">
      <header><strong>Pipeline</strong><span>{{ stages.length || 1 }} stages · {{ duration(elapsed) }}</span></header>
      <div class="ch-stages">
        <div v-if="!stages.length" class="ch-stage" :data-state="status">
          <i></i><strong>batch</strong><span>{{ status }} · {{ duration(elapsed) }}</span>
        </div>
        <div v-for="stage in stages" :key="stage.id" class="ch-stage" :data-state="stage.state" :data-clickable="stage.section" :title="stage.section ? 'Jump to this section in the log' : ''" @click="openStage(stage)">
          <i></i>
          <strong>{{ stage.name }}</strong>
          <span>{{ stage.state }} · {{ duration(stage.elapsed) }}<template v-if="stage.tasks"> · {{ stage.tasks }} task{{ stage.tasks > 1 ? 's' : '' }}</template></span>
          <small>{{ stage.peak }}<em v-if="stage.exit && (stage.exit.return_code || stage.exit.signal)">{{ exitLabel(stage.exit) }}</em></small>
          <div class="ch-stage-bar"><i :style="{ width: `${Math.max(3, stage.share)}%` }"></i></div>
        </div>
      </div>
    </section>

    <section class="ch-panel ch-checks-panel">
      <header><strong>Live checks</strong><span class="ch-check-connection" :data-state="transport.state">{{ transport.label }}</span></header>
      <div v-if="checkGroups.length" class="ch-check-groups">
        <article v-for="group in checkGroups" :key="`${group.step}-${group.task}`" class="ch-check-group">
          <header><strong>Task {{ group.task }}</strong><span>{{ group.node }} · step {{ group.step }}</span></header>
          <div v-for="check in group.checks" :key="check.check_id" class="ch-check" :data-state="effectiveCheckState(check)">
            <i>{{ checkIcon(check) }}</i>
            <div><strong>{{ check.title }}</strong><span>{{ effectiveCheckState(check) === 'stalled' ? `No heartbeat · ${check.message}` : check.message || effectiveCheckState(check) }}</span><progress v-if="checkProgress(check) !== undefined" :value="checkProgress(check)" max="100"></progress></div>
            <time>{{ effectiveCheckState(check) === 'stalled' ? 'STALLED' : check.duration_ms ? duration(check.duration_ms / 1000) : check.progress !== undefined ? `${number(check.progress)}%` : effectiveCheckState(check) }}</time>
          </div>
        </article>
      </div>
      <div v-else class="ch-snippet">
        No structured checks for this job. Emit them from any task with <b>slurm-check</b>; sections fold the log like a CI job:
        <code>{{ CHECK_SNIPPET }}</code>
      </div>
    </section>

    <section class="ch-panel">
      <header><strong>Live GPU allocation</strong><span class="ch-live" :class="{ stale: !allGpus.length }">{{ allGpus.length ? 'LIVE' : isActive ? 'WAITING' : 'FINISHED' }}</span></header>
      <div v-if="allGpus.length" class="ch-gpus">
        <article v-for="entry in allGpus" :key="`${entry.node.node}-${entry.device.uuid}`" class="ch-gpu">
          <div><strong>{{ entry.node.node }} · GPU {{ entry.device.index }}</strong><b>{{ number(entry.device.utilization_gpu) }}%</b></div>
          <progress :value="entry.device.utilization_gpu" max="100"></progress>
          <p><span>VRAM {{ number(entry.device.memory_used_mb / 1024) }} / {{ number(entry.device.memory_total_mb / 1024) }} GiB</span><span>{{ number(entry.device.temperature) }} °C</span><span>{{ number(entry.device.power_watts) }} W</span></p>
        </article>
      </div>
      <p v-else-if="!isActive" class="ch-live-note">Live telemetry is only sampled while the job runs. Peak memory per step is shown in the pipeline above.</p>
      <p v-else-if="!allocatedGpu" class="ch-live-note">This job has no GPU allocated.</p>
      <p v-else class="ch-live-note">Waiting for GPU telemetry from {{ liveJob?.nodes || job.nodes || 'the allocated nodes' }}. The node collector reports every 2 seconds once processes of this job appear on a GPU.</p>
    </section>

    <JobLogViewer
      ref="viewer"
      :model="activeModel"
      :version="logVersion"
      :streams="streamTabs"
      :stream="stream"
      :path="paths[stream] || (merged && stream === 'stderr' ? paths.stdout : '') || 'Output file is not available yet'"
      :live="isActive"
      :paused="paused"
      :transport="transport.label"
      :transport-state="transport.state"
      :error="logError"
      @update:stream="stream = $event"
      @toggle-pause="togglePause"
      @clear="clearView"
      @download="downloadLog"
    />
  </div>
</template>
