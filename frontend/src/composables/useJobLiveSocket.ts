/*
 * Copyright (c) 2026 Rackslab
 *
 * This file is part of Slurm-web.
 *
 * SPDX-License-Identifier: MIT
 */

import { onUnmounted, ref } from 'vue'
import type { Ref } from 'vue'
import { useHttp } from '@/plugins/http'
import { useAuthStore } from '@/stores/auth'
import type {
  JobLiveChannel,
  JobLiveCursors,
  JobLiveServerMessage
} from '@/composables/gateway/types/engineer'

export type JobLiveTransportState = 'connecting' | 'live' | 'reconnecting' | 'fallback' | 'closed'

export interface JobLiveSocketOptions {
  cluster: string
  jobId: () => number
  channels?: JobLiveChannel[]
  /** Called to obtain the cursors to resume from before every (re)connection. */
  cursors: () => JobLiveCursors
  onMessage: (message: JobLiveServerMessage) => void
  /** Called once when the WebSocket transport is given up on and the caller must poll. */
  onFallback?: () => void
}

const MAX_BACKOFF_MS = 10_000
/**
 * Consecutive connections that never reached `ready` before giving up on WebSocket.
 * Only applies while the transport has never worked in this session: once a socket
 * reached `ready`, WebSocket is known to be supported by the deployment and the
 * client keeps reconnecting with backoff through maintenance windows instead.
 */
const FALLBACK_AFTER_FAILURES = 3

/**
 * One multiplexed WebSocket per job carrying checks, log, gpu and job state.
 *
 * Authentication is a first frame `{type: 'auth', token}` so the token never
 * appears in the URL. Every channel resumes from the cursor the caller returns
 * in `cursors()`, so a reconnect is lossless and duplicate free.
 */
export function useJobLiveSocket(options: JobLiveSocketOptions) {
  const http = useHttp()
  const auth = useAuthStore()
  const state: Ref<JobLiveTransportState> = ref('connecting')
  const lastError = ref('')
  let socket: WebSocket | undefined
  let reconnectTimer = -1
  let backoff = 1000
  let failures = 0
  let ready = false
  let everReady = false
  let stopped = false

  function endpoint(): string {
    const base = http.defaults.baseURL || '/api/'
    const absolute = new URL(base, window.location.href)
    absolute.protocol = absolute.protocol === 'https:' ? 'wss:' : 'ws:'
    const path = `${absolute.pathname.replace(/\/$/, '')}/agents/${encodeURIComponent(options.cluster)}/job/${options.jobId()}/live`
    return `${absolute.origin}${path}`
  }

  function send(payload: Record<string, unknown>): boolean {
    if (!socket || socket.readyState !== WebSocket.OPEN) return false
    socket.send(JSON.stringify(payload))
    return true
  }

  function subscribe() {
    const cursors = options.cursors()
    send({
      type: 'subscribe',
      channels: options.channels || ['checks', 'log', 'gpu', 'job'],
      checks_cursor: cursors.checks,
      log: { stream: cursors.log.stream, offset: cursors.log.offset }
    })
  }

  function scheduleReconnect() {
    if (stopped) return
    if (!ready) failures += 1
    if (!everReady && failures >= FALLBACK_AFTER_FAILURES) {
      state.value = 'fallback'
      options.onFallback?.()
      return
    }
    state.value = 'reconnecting'
    window.clearTimeout(reconnectTimer)
    reconnectTimer = window.setTimeout(connect, backoff)
    backoff = Math.min(MAX_BACKOFF_MS, backoff * 2)
  }

  function connect() {
    if (stopped) return
    if (typeof WebSocket === 'undefined') {
      state.value = 'fallback'
      options.onFallback?.()
      return
    }
    ready = false
    if (state.value !== 'reconnecting') state.value = 'connecting'
    try {
      socket = new WebSocket(endpoint())
    } catch (error) {
      lastError.value = error instanceof Error ? error.message : String(error)
      scheduleReconnect()
      return
    }
    const current = socket
    current.onopen = () => {
      current.send(JSON.stringify({ type: 'auth', token: auth.token }))
      subscribe()
    }
    current.onmessage = (event: MessageEvent<string>) => {
      let message: JobLiveServerMessage
      try {
        message = JSON.parse(event.data) as JobLiveServerMessage
      } catch {
        return
      }
      if (message.type === 'ready') {
        ready = true
        everReady = true
        failures = 0
        backoff = 1000
        state.value = 'live'
        lastError.value = ''
      } else if (message.type === 'error') {
        lastError.value = message.message
        if (!message.transient) {
          // Authorization and protocol errors are permanent for this session.
          stopped = message.code === 4401 || message.code === 4403 || message.code === 4404
        }
      }
      options.onMessage(message)
    }
    current.onerror = () => {
      lastError.value = 'WebSocket error'
    }
    current.onclose = (event: CloseEvent) => {
      if (socket !== current) return
      socket = undefined
      if (stopped) {
        state.value = 'closed'
        return
      }
      if (event.code >= 4400 && event.code < 4500 && event.code !== 4502) {
        // Application-level rejection: do not hammer the server.
        lastError.value = event.reason || `closed with ${event.code}`
        stopped = true
        state.value = 'closed'
        return
      }
      scheduleReconnect()
    }
  }

  function close() {
    stopped = true
    window.clearTimeout(reconnectTimer)
    const current = socket
    socket = undefined
    if (current && (current.readyState === WebSocket.OPEN || current.readyState === WebSocket.CONNECTING)) {
      current.close(1000, 'client closed')
    }
    state.value = 'closed'
  }

  /** Drop the current connection and open a fresh one (e.g. after the job id changed). */
  function restart() {
    window.clearTimeout(reconnectTimer)
    stopped = false
    failures = 0
    backoff = 1000
    const current = socket
    socket = undefined
    if (current) current.close(1000, 'restart')
    connect()
  }

  onUnmounted(close)

  return { state, lastError, connect, restart, close, send, subscribe }
}
