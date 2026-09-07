<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import type { SlurmJobDetail, SlurmJobStep } from '@/composables/gateway/slurm/types'
import type { JobGpuTelemetry } from '@/composables/gateway/types/engineer'
import { useGatewayAPI } from '@/composables/GatewayAPI'
import { extractSlurmTRESResources } from '@/composables/gateway/slurm/tres'
import { jobAllocatedGPU } from '@/composables/gateway/slurm/job'
import { fetchEventSource } from '@microsoft/fetch-event-source'
import { useHttp } from '@/plugins/http'
import { useAuthStore } from '@/stores/auth'
import type { JobCheckEvent } from '@/composables/gateway/types/engineer'

const props = defineProps<{ cluster: string; id: number; job: SlurmJobDetail }>()
const gateway = useGatewayAPI()
const http = useHttp()
const auth = useAuthStore()
const output = ref('')
const outputPath = ref('Waiting for output path…')
const offset = ref(0)
const stream = ref<'stdout' | 'stderr'>('stdout')
const paused = ref(false)
const follow = ref(true)
const logElement = ref<HTMLElement>()
const gpu = ref<JobGpuTelemetry>()
const logError = ref('')
const checks = ref<JobCheckEvent[]>([])
const checksCursor = ref(0)
const checksConnection = ref<'connecting' | 'live' | 'reconnecting'>('connecting')
const checksClock = ref(Date.now())
let checksController = new AbortController()
let logTimer = -1
let gpuTimer = -1
let checksClockTimer = -1

const resources = computed(() => extractSlurmTRESResources(props.job.tres.allocated))
const allocatedGpu = computed(() => jobAllocatedGPU(props.job))
const elapsed = computed(() => props.job.time.elapsed || 0)
const limitSeconds = computed(() =>
  props.job.time.limit?.set && !props.job.time.limit.infinite ? props.job.time.limit.number * 60 : 0
)
const progress = computed(() =>
  limitSeconds.value ? Math.min(100, (elapsed.value / limitSeconds.value) * 100) : 0
)
const gpuHours = computed(() => (allocatedGpu.value * elapsed.value) / 3600)
const cpuHours = computed(() => ((resources.value.cpu || 0) * elapsed.value) / 3600)
const status = computed(() => props.job.state.current[0] || 'UNKNOWN')
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

async function loadChecks() {
  const snapshot = await gateway.jobChecks(props.cluster, props.id)
  checks.value = snapshot.checks
  checksCursor.value = snapshot.cursor
}

async function startChecks() {
  checksController.abort()
  checksController = new AbortController()
  checksConnection.value = 'connecting'
  try {
    await loadChecks()
    const base = http.defaults.baseURL || '/api/'
    await fetchEventSource(
      `${base}agents/${encodeURIComponent(props.cluster)}/job/${props.id}/checks/events?cursor=${checksCursor.value}`,
      {
        headers: { Authorization: `Bearer ${auth.token}` },
        signal: checksController.signal,
        openWhenHidden: true,
        onopen: async (response) => {
          if (!response.ok) throw new Error(`Checks stream HTTP ${response.status}`)
          checksConnection.value = 'live'
        },
        onmessage: (message) => {
          if (message.event === 'check' && message.data) upsertCheck(JSON.parse(message.data) as JobCheckEvent)
        },
        onclose: () => {
          checksConnection.value = 'reconnecting'
          throw new Error('Checks stream closed')
        },
        onerror: () => {
          checksConnection.value = 'reconnecting'
          return 1000
        }
      }
    )
  } catch (error) {
    if (!checksController.signal.aborted) checksConnection.value = 'reconnecting'
  }
}

async function pollLog() {
  if (paused.value) return
  try {
    const chunk = await gateway.jobLog(props.cluster, props.id, stream.value, offset.value)
    outputPath.value = chunk.path || 'Output file is not available yet'
    logError.value = ''
    if (chunk.rotated) {
      offset.value = 0
      output.value = '[output rotated]\n'
    }
    if (chunk.chunk) {
      output.value += chunk.chunk
      offset.value = chunk.next_offset
      if (output.value.length > 2_000_000) output.value = output.value.slice(-1_500_000)
      if (follow.value) {
        await nextTick()
        if (logElement.value) logElement.value.scrollTop = logElement.value.scrollHeight
      }
    }
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

function resetLog() {
  offset.value = 0
  output.value = ''
  void pollLog()
}

watch(stream, resetLog)
watch(() => props.id, resetLog)
watch(() => props.id, startChecks)

onMounted(() => {
  void pollLog()
  void pollGpu()
  void startChecks()
  logTimer = window.setInterval(pollLog, 1000)
  gpuTimer = window.setInterval(pollGpu, 2000)
  checksClockTimer = window.setInterval(() => (checksClock.value = Date.now()), 1000)
})

onUnmounted(() => {
  window.clearInterval(logTimer)
  window.clearInterval(gpuTimer)
  window.clearInterval(checksClockTimer)
  gateway.abort()
  checksController.abort()
})
</script>

<template>
  <div class="ch-workbench">
    <div class="ch-run-header">
      <div>
        <p class="ch-eyebrow">PIPELINE RUN · #{{ id }}</p>
        <h1>{{ job.name }}</h1>
        <p class="ch-subtitle">{{ job.user }} · {{ job.partition }} · {{ job.nodes || 'Waiting for allocation' }}</p>
      </div>
      <span class="ch-status" :data-state="status">{{ status }}</span>
    </div>

    <div class="ch-metrics">
      <div class="ch-metric"><span>Elapsed</span><strong>{{ duration(elapsed) }}</strong><small v-if="limitSeconds">{{ number(progress) }}% of {{ duration(limitSeconds) }}</small></div>
      <div class="ch-metric"><span>GPU consumption</span><strong>{{ number(gpuHours, 2) }} GPU-h</strong><small>{{ allocatedGpu }} GPU allocated</small></div>
      <div class="ch-metric"><span>CPU consumption</span><strong>{{ number(cpuHours, 2) }} CPU-h</strong><small>{{ resources.cpu }} CPU allocated</small></div>
      <div class="ch-metric"><span>Live GPU util</span><strong>{{ number(gpu?.summary.utilization || 0) }}%</strong><small>{{ gpu?.summary.count || 0 }} GPU reporting</small></div>
      <div class="ch-metric"><span>GPU memory</span><strong>{{ number((gpu?.summary.memory_used_mb || 0) / 1024) }} GiB</strong><small>of {{ number((gpu?.summary.memory_total_mb || 0) / 1024) }} GiB</small></div>
      <div class="ch-metric"><span>Power</span><strong>{{ number(gpu?.summary.power_watts || 0) }} W</strong><small>max {{ number(gpu?.summary.temperature_max || 0) }} °C</small></div>
    </div>

    <section class="ch-panel">
      <header><strong>Pipeline</strong><span>{{ job.steps.length || 1 }} stages · {{ duration(elapsed) }}</span></header>
      <div class="ch-stages">
        <div v-if="!job.steps.length" class="ch-stage" :data-state="status">
          <i></i><strong>batch</strong><span>{{ status }} · {{ duration(elapsed) }}</span>
        </div>
        <div v-for="step in job.steps" :key="step.step.id" class="ch-stage" :data-state="stepStatus(step)">
          <i></i><strong>{{ stepName(step) }}</strong><span>{{ stepStatus(step) }} · {{ duration(step.time.elapsed) }}</span><small>{{ stepPeakMemory(step) }}</small>
        </div>
      </div>
    </section>

    <section class="ch-panel ch-checks-panel">
      <header><strong>Live checks</strong><span class="ch-check-connection" :data-state="checksConnection">{{ checksConnection.toUpperCase() }}</span></header>
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
      <p v-else class="ch-empty">No structured checks emitted yet. Raw pipeline output remains live.</p>
    </section>

    <section class="ch-panel">
      <header><strong>Live GPU allocation</strong><span class="ch-live" :class="{ stale: !allGpus.length }">{{ allGpus.length ? 'LIVE' : 'WAITING' }}</span></header>
      <div v-if="allGpus.length" class="ch-gpus">
        <article v-for="entry in allGpus" :key="`${entry.node.node}-${entry.device.uuid}`" class="ch-gpu">
          <div><strong>{{ entry.node.node }} · GPU {{ entry.device.index }}</strong><b>{{ number(entry.device.utilization_gpu) }}%</b></div>
          <progress :value="entry.device.utilization_gpu" max="100"></progress>
          <p><span>VRAM {{ number(entry.device.memory_used_mb / 1024) }} / {{ number(entry.device.memory_total_mb / 1024) }} GiB</span><span>{{ number(entry.device.temperature) }} °C</span><span>{{ number(entry.device.power_watts) }} W</span></p>
        </article>
      </div>
      <p v-else class="ch-empty">No fresh GPU process telemetry for this job.</p>
    </section>

    <section class="ch-panel ch-terminal-panel">
      <header class="ch-terminal-toolbar">
        <div><span class="ch-window-dots">● ● ●</span><strong>Pipeline output</strong></div>
        <div class="ch-actions">
          <select v-model="stream"><option value="stdout">stdout</option><option value="stderr">stderr</option></select>
          <label><input v-model="follow" type="checkbox" /> Follow</label>
          <button type="button" @click="paused = !paused">{{ paused ? 'Resume' : 'Pause' }}</button>
          <button type="button" @click="output = ''; offset = 0">Clear view</button>
        </div>
      </header>
      <div class="ch-log-path">{{ outputPath }}</div>
      <pre ref="logElement" class="ch-log"><code>{{ output || (logError ? `Log error: ${logError}` : 'Waiting for pipeline output…') }}</code></pre>
    </section>
  </div>
</template>
