<!--
  Copyright (c) 2026 Rackslab

  This file is part of Slurm-web.

  SPDX-License-Identifier: MIT
-->

<script setup lang="ts">
/*
 * Workload activity for the dashboard, built only from the scheduler and
 * accounting APIs: queue composition, last 24 hours outcomes, the most recent
 * failures and what is running right now.
 */
import { computed, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { useClusterDataPoller } from '@/composables/DataPoller'
import type { SlurmAcctJob, SlurmJob } from '@/composables/gateway/slurm/types'
import JobStatusBadge from '@/components/job/JobStatusBadge.vue'

const { cluster } = defineProps<{ cluster: string }>()

const FAILURE_STATES = ['FAILED', 'OUT_OF_MEMORY', 'NODE_FAIL', 'TIMEOUT', 'BOOT_FAIL', 'DEADLINE']
const PAST_HOURS = 24

const active = useClusterDataPoller<SlurmJob[]>(cluster, 'jobs', 10000)
const past = useClusterDataPoller<SlurmAcctJob[]>(cluster, 'jobsPast', 60000, PAST_HOURS)

watch(
  () => cluster,
  (value) => {
    active.setCluster(value)
    past.setCluster(value)
  }
)

function acctSeconds(value: SlurmAcctJob['time']['end']): number {
  if (typeof value === 'number') return value
  if (value && typeof value === 'object' && value.set) return value.number
  return 0
}

const queue = computed(() => {
  const jobs = active.data.value ?? []
  const count = (state: string) => jobs.filter((job) => job.job_state.includes(state)).length
  const running = count('RUNNING')
  const pending = count('PENDING')
  return { total: jobs.length, running, pending, other: jobs.length - running - pending }
})

const outcomes = computed(() => {
  const jobs = past.data.value ?? []
  const has = (job: SlurmAcctJob, states: string[]) => job.state.current.some((s) => states.includes(s))
  const failed = jobs.filter((job) => has(job, FAILURE_STATES)).length
  const cancelled = jobs.filter((job) => has(job, ['CANCELLED'])).length
  const completed = jobs.filter((job) => has(job, ['COMPLETED'])).length
  const total = jobs.length
  return { total, completed, failed, cancelled, rate: total ? Math.round((failed / total) * 100) : 0 }
})

const recentFailures = computed(() =>
  (past.data.value ?? [])
    .filter((job) => job.state.current.some((s) => FAILURE_STATES.includes(s)))
    .sort((a, b) => acctSeconds(b.time.end) - acctSeconds(a.time.end))
    .slice(0, 6)
)

const running = computed(() =>
  (active.data.value ?? [])
    .filter((job) => job.job_state.includes('RUNNING'))
    .sort((a, b) => a.job_id - b.job_id)
    .slice(0, 8)
)

const pending = computed(() =>
  (active.data.value ?? []).filter((job) => job.job_state.includes('PENDING')).slice(0, 5)
)

function ago(seconds: number): string {
  if (!seconds) return ''
  const delta = Math.max(0, Math.floor(Date.now() / 1000 - seconds))
  if (delta < 60) return `${delta}s ago`
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`
  return `${Math.floor(delta / 86400)}d ago`
}

function elapsed(job: SlurmJob): string {
  const start = (job as unknown as { start_time?: { set: boolean; number: number } }).start_time
  if (!start?.set) return ''
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - start.number))
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return h ? `${h}h ${m}m` : `${m}m`
}
</script>

<template>
  <div class="ch-activity">
    <section class="ch-panel">
      <header><strong>Queue now</strong><span>{{ queue.total }} in queue</span></header>
      <div class="ch-activity-stats">
        <div><span>Running</span><strong data-tone="running">{{ queue.running }}</strong></div>
        <div><span>Pending</span><strong data-tone="pending">{{ queue.pending }}</strong></div>
        <div><span>Other</span><strong>{{ queue.other }}</strong></div>
      </div>
      <table v-if="running.length" class="ch-activity-table">
        <tbody>
          <tr v-for="job in running" :key="job.job_id">
            <td class="ch-mono"><RouterLink :to="{ name: 'job', params: { cluster, id: job.job_id } }">#{{ job.job_id }}</RouterLink></td>
            <td class="ch-ellipsis">{{ job.name || '∅' }}</td>
            <td>{{ job.user_name }}</td>
            <td class="ch-mono">{{ job.nodes }}</td>
            <td class="ch-mono ch-right">{{ elapsed(job) }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="ch-live-note">Nothing is running. <template v-if="pending.length">{{ pending.length }} job{{ pending.length > 1 ? 's' : '' }} waiting: {{ pending.map((j) => `#${j.job_id} ${j.state_reason}`).join(', ') }}.</template></p>
    </section>

    <section class="ch-panel">
      <header><strong>Last 24 hours</strong><span>{{ outcomes.total }} finished</span></header>
      <div class="ch-activity-stats">
        <div><span>Completed</span><strong data-tone="ok">{{ outcomes.completed }}</strong></div>
        <div><span>Failed</span><strong :data-tone="outcomes.failed ? 'bad' : ''">{{ outcomes.failed }}</strong></div>
        <div><span>Cancelled</span><strong>{{ outcomes.cancelled }}</strong></div>
        <div><span>Failure rate</span><strong :data-tone="outcomes.rate >= 20 ? 'bad' : ''">{{ outcomes.rate }}%</strong></div>
      </div>
      <table v-if="recentFailures.length" class="ch-activity-table">
        <tbody>
          <tr v-for="job in recentFailures" :key="job.job_id">
            <td class="ch-mono"><RouterLink :to="{ name: 'job', params: { cluster, id: job.job_id } }">#{{ job.job_id }}</RouterLink></td>
            <td class="ch-ellipsis">{{ job.name || '∅' }}</td>
            <td>{{ job.user }}</td>
            <td><JobStatusBadge :status="job.state.current" /></td>
            <td class="ch-mono ch-right">{{ ago(acctSeconds(job.time.end)) }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="ch-live-note">No failed jobs in the last 24 hours.</p>
      <footer class="ch-activity-footer">
        <RouterLink :to="{ name: 'jobs-past', params: { cluster } }">All terminated jobs →</RouterLink>
      </footer>
    </section>
  </div>
</template>
