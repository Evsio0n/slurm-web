/*
 * Copyright (c) 2026 Rackslab
 *
 * This file is part of Slurm-web.
 *
 * SPDX-License-Identifier: MIT
 */

/**
 * Incremental parser turning raw job output into CI-style log lines: ANSI SGR
 * colors, GitLab (`section_start:ts:name`) and GitHub (`::group::`) collapsible
 * sections, leading timestamps and error/warning classification.
 */

export interface LogSegment {
  text: string
  /** Space separated classes such as `a-fg-31 a-bold`. */
  cls?: string
  style?: string
}

export type LogLevel = 'error' | 'warn' | undefined

export interface LogLine {
  /** 1-based absolute line number in the stream. */
  n: number
  ts?: string
  raw: string
  segments: LogSegment[]
  level: LogLevel
  /** Section name this line belongs to, if any. */
  section?: string
  /** True for the synthetic section header row. */
  header?: boolean
}

export interface LogSection {
  name: string
  title: string
  /** Line number of the header row. */
  firstLine: number
  /** Last line number inside the section (updated while open). */
  lastLine: number
  startTs?: number
  endTs?: number
  collapsed: boolean
  hasError: boolean
  open: boolean
}

export const ERROR_RE =
  /(traceback \(most recent call last\)|\berror\b|exception|cuda out of memory|out of memory|oom-kill|\bkilled\b|segmentation fault|core dumped|assertion|srun: error|slurmstepd: error|\bfatal\b|\bpanic\b|command not found|no such file|permission denied|non-zero exit)/i
export const WARN_RE = /(\bwarn(ing)?\b|deprecat|retry(ing)?\b)/i

const TS_RE = /^\[?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\]?\s?/
const GITLAB_START = /\x1b\[0Ksection_start:(\d+):([\w.-]+)(\[[^\]]*\])?\r\x1b\[0K/
const GITLAB_END = /\x1b\[0Ksection_end:(\d+):([\w.-]+)\r\x1b\[0K/
const GITHUB_START = /^::group::(.*)$/
const GITHUB_END = /^::endgroup::\s*$/
// CSI sequences other than SGR, OSC sequences and stray control bytes.
const STRIP_RE = /\x1b\[[0-9;?]*[A-LN-Zln-z]|\x1b\][^\x07]*(\x07|\x1b\\)|\x1b[()][A-Z0-9]|[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f]/g
const SGR_RE = /\x1b\[([0-9;]*)m/g

const XTERM_BASE = [
  '#000000', '#cd3131', '#0dbc79', '#e5e510', '#2472c8', '#bc3fbc', '#11a8cd', '#e5e5e5',
  '#666666', '#f14c4c', '#23d18b', '#f5f543', '#3b8eea', '#d670d6', '#29b8db', '#ffffff'
]

function xterm256(n: number): string {
  if (n < 16) return XTERM_BASE[n]
  if (n >= 232) {
    const v = 8 + (n - 232) * 10
    return `rgb(${v},${v},${v})`
  }
  const i = n - 16
  const r = Math.floor(i / 36)
  const g = Math.floor((i % 36) / 6)
  const b = i % 6
  const c = (x: number) => (x ? 55 + x * 40 : 0)
  return `rgb(${c(r)},${c(g)},${c(b)})`
}

interface SgrState {
  bold: boolean
  dim: boolean
  italic: boolean
  underline: boolean
  fg?: string
  bg?: string
  fgStyle?: string
  bgStyle?: string
}

function applySgr(state: SgrState, params: string): SgrState {
  const next = { ...state }
  const codes = params === '' ? [0] : params.split(';').map((v) => parseInt(v, 10) || 0)
  for (let i = 0; i < codes.length; i++) {
    const code = codes[i]
    if (code === 0) return { bold: false, dim: false, italic: false, underline: false }
    else if (code === 1) next.bold = true
    else if (code === 2) next.dim = true
    else if (code === 3) next.italic = true
    else if (code === 4) next.underline = true
    else if (code === 22) (next.bold = false), (next.dim = false)
    else if (code === 23) next.italic = false
    else if (code === 24) next.underline = false
    else if (code === 39) (next.fg = undefined), (next.fgStyle = undefined)
    else if (code === 49) (next.bg = undefined), (next.bgStyle = undefined)
    else if ((code >= 30 && code <= 37) || (code >= 90 && code <= 97)) {
      next.fg = `a-fg-${code}`
      next.fgStyle = undefined
    } else if ((code >= 40 && code <= 47) || (code >= 100 && code <= 107)) {
      next.bg = `a-bg-${code}`
      next.bgStyle = undefined
    } else if (code === 38 || code === 48) {
      const target = code === 38 ? 'fg' : 'bg'
      let color: string | undefined
      if (codes[i + 1] === 5) {
        color = xterm256(codes[i + 2] || 0)
        i += 2
      } else if (codes[i + 1] === 2) {
        color = `rgb(${codes[i + 2] || 0},${codes[i + 3] || 0},${codes[i + 4] || 0})`
        i += 4
      }
      if (color) {
        if (target === 'fg') (next.fg = undefined), (next.fgStyle = `color:${color}`)
        else (next.bg = undefined), (next.bgStyle = `background:${color}`)
      }
    }
  }
  return next
}

function segmentsFor(text: string): LogSegment[] {
  // Carriage-return progress bars: keep only what was painted last.
  if (text.includes('\r')) {
    const parts = text.split('\r')
    text = parts[parts.length - 1] || parts[parts.length - 2] || ''
  }
  text = text.replace(STRIP_RE, '')
  if (!text.includes('\x1b[')) return text ? [{ text }] : []
  const segments: LogSegment[] = []
  let state: SgrState = { bold: false, dim: false, italic: false, underline: false }
  let last = 0
  SGR_RE.lastIndex = 0
  let match: RegExpExecArray | null
  const push = (chunk: string) => {
    if (!chunk) return
    const cls = [
      state.bold ? 'a-bold' : '',
      state.dim ? 'a-dim' : '',
      state.italic ? 'a-italic' : '',
      state.underline ? 'a-underline' : '',
      state.fg || '',
      state.bg || ''
    ]
      .filter(Boolean)
      .join(' ')
    const style = [state.fgStyle, state.bgStyle].filter(Boolean).join(';')
    segments.push({ text: chunk, cls: cls || undefined, style: style || undefined })
  }
  while ((match = SGR_RE.exec(text)) !== null) {
    push(text.slice(last, match.index))
    state = applySgr(state, match[1])
    last = match.index + match[0].length
  }
  push(text.slice(last))
  return segments
}

export function classify(raw: string): LogLevel {
  if (ERROR_RE.test(raw)) return 'error'
  if (WARN_RE.test(raw)) return 'warn'
  return undefined
}

export class LogModel {
  lines: LogLine[] = []
  sections: LogSection[] = []
  errorLines = 0
  warnLines = 0
  private partial = ''
  private nextNumber = 1
  private openSections: LogSection[] = []
  /** Incremented on every change so shallow reactivity can track it. */
  version = 0

  reset() {
    this.lines = []
    this.sections = []
    this.errorLines = 0
    this.warnLines = 0
    this.partial = ''
    this.nextNumber = 1
    this.openSections = []
    this.version++
  }

  /** Feed a raw chunk. Returns the line numbers added. */
  append(chunk: string): number {
    if (!chunk) return 0
    const text = this.partial + chunk
    const parts = text.split('\n')
    this.partial = parts.pop() || ''
    for (const part of parts) this.pushRaw(part)
    this.version++
    return parts.length
  }

  /** Flush an unterminated last line (e.g. when the job finished). */
  flush() {
    if (!this.partial) return
    this.pushRaw(this.partial)
    this.partial = ''
    this.version++
  }

  private pushRaw(raw: string) {
    let rest = raw
    // Section markers may share a line with content; peel them off first.
    let guard = 0
    while (guard++ < 8) {
      const start = GITLAB_START.exec(rest)
      const end = GITLAB_END.exec(rest)
      if (start && (!end || start.index <= end.index)) {
        const before = rest.slice(0, start.index)
        if (before.trim()) this.pushLine(before)
        rest = rest.slice(start.index + start[0].length)
        const title = rest.split('\x1b')[0]
        rest = rest.slice(title.length)
        this.openSection(start[2], title.trim() || start[2], parseInt(start[1], 10), /collapsed=true/.test(start[3] || ''))
        continue
      }
      if (end) {
        const before = rest.slice(0, end.index)
        if (before.trim()) this.pushLine(before)
        rest = rest.slice(end.index + end[0].length)
        this.closeSection(end[2], parseInt(end[1], 10))
        continue
      }
      break
    }
    const gh = GITHUB_START.exec(rest)
    if (gh) {
      const name = `group-${this.nextNumber}`
      this.openSection(name, gh[1].trim() || name, undefined, false)
      return
    }
    if (GITHUB_END.test(rest)) {
      const current = this.openSections[this.openSections.length - 1]
      if (current) this.closeSection(current.name, undefined)
      return
    }
    if (rest === '' && raw !== '') return
    this.pushLine(rest)
  }

  private pushLine(raw: string) {
    let ts: string | undefined
    let body = raw
    const tsMatch = TS_RE.exec(raw)
    if (tsMatch) {
      ts = tsMatch[1]
      body = raw.slice(tsMatch[0].length)
    }
    const level = classify(raw)
    if (level === 'error') this.errorLines++
    else if (level === 'warn') this.warnLines++
    const section = this.openSections[this.openSections.length - 1]
    const line: LogLine = {
      n: this.nextNumber++,
      ts,
      raw,
      segments: segmentsFor(body),
      level,
      section: section?.name
    }
    if (section) {
      section.lastLine = line.n
      if (level === 'error') section.hasError = true
    }
    this.lines.push(line)
  }

  private openSection(name: string, title: string, startTs: number | undefined, collapsed: boolean) {
    const section: LogSection = {
      name,
      title,
      firstLine: this.nextNumber,
      lastLine: this.nextNumber,
      startTs,
      collapsed,
      hasError: false,
      open: true
    }
    this.sections.push(section)
    this.openSections.push(section)
    this.lines.push({
      n: this.nextNumber++,
      raw: title,
      segments: [{ text: title }],
      level: undefined,
      section: name,
      header: true
    })
  }

  private closeSection(name: string, endTs: number | undefined) {
    const index = this.openSections.findIndex((s) => s.name === name)
    if (index === -1) return
    const [section] = this.openSections.splice(index, 1)
    section.open = false
    section.endTs = endTs
  }
}

export function sectionDuration(section: LogSection, nowSeconds: number): number | undefined {
  if (section.startTs === undefined) return undefined
  return (section.endTs ?? nowSeconds) - section.startTs
}
