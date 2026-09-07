<script setup lang="ts">
/*
 * CI-style job log viewer: numbered, anchorable lines, collapsible sections,
 * ANSI colors, error highlighting, search, follow and raw download. Modelled
 * on the GitLab and GitHub Actions job log panes.
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import type { LogModel, LogLine, LogSection, LogSegment } from '@/composables/logParser'
import { sectionDuration } from '@/composables/logParser'

export interface LogStreamTab {
  id: 'stdout' | 'stderr'
  lines: number
  errors: number
  merged?: boolean
}

const props = defineProps<{
  model: LogModel
  version: number
  streams: LogStreamTab[]
  stream: 'stdout' | 'stderr'
  path: string
  live: boolean
  /** Unix seconds when the job ended; freezes durations of sections left open. */
  endedAt?: number
  paused: boolean
  transport: string
  transportState: string
  error?: string
}>()

const emit = defineEmits<{
  (e: 'update:stream', value: 'stdout' | 'stderr'): void
  (e: 'toggle-pause'): void
  (e: 'clear'): void
  (e: 'download'): void
}>()

const container = ref<HTMLElement>()
const follow = ref(true)
const wrap = ref(true)
const showTimestamps = ref(true)
const query = ref('')
const matchIndex = ref(0)
const activeLine = ref<number>()
const collapsed = ref<Set<string>>(new Set())
const WINDOW = 3000
const windowSize = ref(WINDOW)
const now = ref(Math.floor(Date.now() / 1000))
let clock = -1
let programmaticScroll = false

/* Lines & sections (re-evaluated when the model version changes). */
const allLines = computed(() => {
  void props.version
  return props.model.lines
})
const sections = computed(() => {
  void props.version
  return props.model.sections
})
const sectionByName = computed(() => new Map(sections.value.map((s) => [s.name, s])))
const hiddenCount = computed(() => Math.max(0, allLines.value.length - windowSize.value))
const visibleLines = computed(() => {
  const lines = hiddenCount.value ? allLines.value.slice(hiddenCount.value) : allLines.value
  if (!collapsed.value.size) return lines
  return lines.filter((line) => line.header || !line.section || !collapsed.value.has(line.section))
})
const matches = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return [] as number[]
  const out: number[] = []
  for (const line of allLines.value) if (line.raw.toLowerCase().includes(q)) out.push(line.n)
  return out
})
const errorLines = computed(() => {
  void props.version
  return props.model.errorLines
})

function isCollapsed(section: LogSection) {
  return collapsed.value.has(section.name)
}

function toggleSection(name: string) {
  const next = new Set(collapsed.value)
  if (next.has(name)) next.delete(name)
  else next.add(name)
  collapsed.value = next
}

function duration(seconds: number | undefined) {
  if (seconds === undefined || !Number.isFinite(seconds)) return ''
  const s = Math.max(0, Math.floor(seconds))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${s % 60}s`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

function sectionMeta(name: string | undefined) {
  if (!name) return undefined
  const section = sectionByName.value.get(name)
  if (!section) return undefined
  return { section, duration: duration(sectionDuration(section, props.endedAt ?? now.value)) }
}

/* Highlight search matches inside a line's segments. */
function highlighted(segments: LogSegment[]): LogSegment[] {
  const q = query.value.trim().toLowerCase()
  if (!q) return segments
  const out: LogSegment[] = []
  for (const seg of segments) {
    const lower = seg.text.toLowerCase()
    let from = 0
    let at = lower.indexOf(q)
    if (at === -1) {
      out.push(seg)
      continue
    }
    while (at !== -1) {
      if (at > from) out.push({ ...seg, text: seg.text.slice(from, at) })
      out.push({ ...seg, text: seg.text.slice(at, at + q.length), cls: `${seg.cls || ''} a-mark`.trim() })
      from = at + q.length
      at = lower.indexOf(q, from)
    }
    if (from < seg.text.length) out.push({ ...seg, text: seg.text.slice(from) })
  }
  return out
}

/* Scrolling */
function scrollToBottom() {
  const el = container.value
  if (!el) return
  programmaticScroll = true
  el.scrollTop = el.scrollHeight
  window.setTimeout(() => (programmaticScroll = false), 50)
}

function scrollToTop() {
  follow.value = false
  const el = container.value
  if (el) el.scrollTop = 0
}

async function scrollToLine(n: number, highlight = true, reveal = false) {
  follow.value = false
  if (reveal) container.value?.closest('section')?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  if (n <= hiddenCount.value) windowSize.value = allLines.value.length - n + WINDOW
  const line = allLines.value[n - 1]
  if (line?.section && collapsed.value.has(line.section)) toggleSection(line.section)
  if (highlight) activeLine.value = n
  await nextTick()
  const el = container.value?.querySelector<HTMLElement>(`[data-line="${n}"]`)
  if (el && container.value) {
    programmaticScroll = true
    container.value.scrollTop = el.offsetTop - container.value.clientHeight / 3
    window.setTimeout(() => (programmaticScroll = false), 50)
  }
}

function scrollToSection(name: string) {
  const section = sectionByName.value.get(name)
  if (!section) return false
  void scrollToLine(section.firstLine, false, true)
  return true
}

function scrollToFirstError() {
  const line = allLines.value.find((l) => l.level === 'error')
  if (line) void scrollToLine(line.n)
}

function scrollToLastError() {
  for (let i = allLines.value.length - 1; i >= 0; i--) {
    if (allLines.value[i].level === 'error') {
      void scrollToLine(allLines.value[i].n)
      return
    }
  }
}

function onScroll() {
  if (programmaticScroll) return
  const el = container.value
  if (!el) return
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24
  if (atBottom && !follow.value) follow.value = true
  else if (!atBottom && follow.value) follow.value = false
}

function toggleFollow() {
  follow.value = !follow.value
  if (follow.value) scrollToBottom()
}

function gotoMatch(delta: number) {
  if (!matches.value.length) return
  matchIndex.value = (matchIndex.value + delta + matches.value.length) % matches.value.length
  void scrollToLine(matches.value[matchIndex.value])
}

function selectLine(n: number) {
  activeLine.value = n
  history.replaceState(null, '', `#L${n}`)
}

function copyPath() {
  void navigator.clipboard?.writeText(props.path)
}

watch(matches, (list) => {
  matchIndex.value = 0
  if (list.length) void scrollToLine(list[0])
})

watch(
  () => props.version,
  async () => {
    if (!follow.value) return
    await nextTick()
    scrollToBottom()
  }
)

watch(
  () => props.stream,
  () => {
    windowSize.value = WINDOW
    activeLine.value = undefined
    follow.value = true
  }
)

/* Collapse sections that asked for it as they appear. */
watch(sections, (list) => {
  const next = new Set(collapsed.value)
  let changed = false
  for (const section of list) {
    if (section.collapsed && !section.open && !next.has(section.name) && !section.hasError) {
      next.add(section.name)
      changed = true
    }
  }
  if (changed) collapsed.value = next
})

onMounted(() => {
  clock = window.setInterval(() => (now.value = Math.floor(Date.now() / 1000)), 1000)
  const hash = /^#L(\d+)$/.exec(window.location.hash)
  if (hash) window.setTimeout(() => void scrollToLine(parseInt(hash[1], 10)), 300)
})

onUnmounted(() => window.clearInterval(clock))

defineExpose({ scrollToLine, scrollToSection, scrollToFirstError, scrollToLastError })
</script>

<template>
  <section class="ch-panel ch-terminal-panel">
    <header class="ch-terminal-toolbar">
      <div class="ch-terminal-tabs">
        <button
          v-for="tab in streams"
          :key="tab.id"
          type="button"
          class="ch-terminal-tab"
          :data-active="tab.id === stream"
          @click="emit('update:stream', tab.id)"
        >
          {{ tab.id }}
          <span v-if="tab.merged" class="ch-tab-note" title="Slurm merged stderr into stdout for this job">merged</span>
          <template v-else>
            <span class="ch-tab-count">{{ tab.lines }}</span>
            <span v-if="tab.errors" class="ch-tab-errors" :title="`${tab.errors} error lines`">{{ tab.errors }}</span>
          </template>
        </button>
        <span class="ch-terminal-state" :data-state="paused ? 'paused' : transportState">{{ paused ? 'PAUSED' : live ? 'STREAMING' : transport }}</span>
      </div>
      <div class="ch-actions">
        <label class="ch-search">
          <input v-model="query" type="search" placeholder="Search log…" spellcheck="false" @keydown.enter.prevent="gotoMatch($event.shiftKey ? -1 : 1)" />
          <span v-if="query" class="ch-search-count">{{ matches.length ? matchIndex + 1 : 0 }}/{{ matches.length }}</span>
          <button v-if="query" type="button" title="Previous match (Shift+Enter)" @click="gotoMatch(-1)">↑</button>
          <button v-if="query" type="button" title="Next match (Enter)" @click="gotoMatch(1)">↓</button>
        </label>
        <button v-if="errorLines" type="button" class="ch-btn-error" :title="`${errorLines} error lines · click for the last one`" @click="scrollToLastError">! {{ errorLines }}</button>
        <button type="button" :data-on="follow" title="Follow output" @click="toggleFollow">Follow</button>
        <button type="button" :data-on="showTimestamps" title="Show timestamps" @click="showTimestamps = !showTimestamps">Time</button>
        <button type="button" :data-on="wrap" title="Wrap long lines" @click="wrap = !wrap">Wrap</button>
        <button type="button" title="Scroll to top" @click="scrollToTop">⤒</button>
        <button type="button" title="Scroll to bottom" @click="follow = true; scrollToBottom()">⤓</button>
        <button type="button" @click="emit('toggle-pause')">{{ paused ? 'Resume' : 'Pause' }}</button>
        <button type="button" title="Download raw log" @click="emit('download')">Raw</button>
        <button type="button" @click="emit('clear')">Clear</button>
      </div>
    </header>
    <div class="ch-log-path" :title="path">
      <button type="button" class="ch-copy" title="Copy path" @click="copyPath">⧉</button>
      <span>{{ path }}</span>
    </div>
    <div ref="container" class="ch-log" :data-wrap="wrap" @scroll.passive="onScroll">
      <button v-if="hiddenCount" type="button" class="ch-log-more" @click="windowSize += WINDOW">
        Show {{ Math.min(WINDOW, hiddenCount).toLocaleString() }} earlier lines ({{ hiddenCount.toLocaleString() }} hidden)
      </button>
      <template v-for="line in visibleLines" :key="line.n">
        <div
          v-if="line.header"
          class="ch-log-section"
          :data-line="line.n"
          :data-open="!isCollapsed(sectionMeta(line.section)!.section)"
          :data-error="sectionMeta(line.section)?.section.hasError"
          :data-running="sectionMeta(line.section)?.section.open && live"
          @click="toggleSection(line.section!)"
        >
          <span class="ch-log-n">{{ line.n }}</span>
          <i>{{ isCollapsed(sectionMeta(line.section)!.section) ? '▸' : '▾' }}</i>
          <strong>{{ line.raw }}</strong>
          <time>{{ sectionMeta(line.section)?.duration }}</time>
        </div>
        <div
          v-else
          class="ch-log-line"
          :data-line="line.n"
          :data-level="line.level"
          :data-active="line.n === activeLine"
          :data-in-section="!!line.section"
        >
          <button type="button" class="ch-log-n" @click="selectLine(line.n)">{{ line.n }}</button>
          <time v-if="showTimestamps && line.ts">{{ line.ts }}</time>
          <span class="ch-log-text"><span v-for="(seg, i) in highlighted(line.segments)" :key="i" :class="seg.cls" :style="seg.style">{{ seg.text }}</span></span>
        </div>
      </template>
      <div v-if="!allLines.length" class="ch-log-empty">
        {{ error ? `Log error: ${error}` : live ? 'Waiting for output…' : 'No output was written to this stream.' }}
      </div>
    </div>
  </section>
</template>
