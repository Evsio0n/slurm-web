<!--
  Copyright (c) 2026 Rackslab

  This file is part of Slurm-web.

  SPDX-License-Identifier: MIT
-->

<script setup lang="ts">
/*
 * Compact jobs page header: Active / Terminated tabs, live count, name search
 * and, for terminated jobs, the time range presets. Replaces the tall title
 * block so the list starts higher on the page.
 */
import { RouterLink } from 'vue-router'
import { useRuntimeStore } from '@/stores/runtime'
import JobsPastTimeRangeSelector from '@/components/jobs/JobsPastTimeRangeSelector.vue'

const { cluster, mode, count, loaded, maxHours, defaultHours } = defineProps<{
  cluster: string
  mode: 'active' | 'past'
  count: number
  loaded: boolean
  maxHours?: number
  defaultHours?: number
}>()

const emit = defineEmits<{
  pastHours: [hours: number]
}>()

const runtimeStore = useRuntimeStore()
</script>

<template>
  <div class="ch-jobs-header">
    <div class="ch-jobs-tabs" role="tablist">
      <RouterLink
        v-if="runtimeStore.hasAnyPermission(['jobs-view', 'jobs-view-own'])"
        :to="{ name: 'jobs', params: { cluster } }"
        class="ch-jobs-tab"
        :data-active="mode === 'active'"
        role="tab"
        >Active</RouterLink
      >
      <RouterLink
        v-if="runtimeStore.hasAnyPermission(['jobs-view-past', 'jobs-view-past-own'])"
        :to="{ name: 'jobs-past', params: { cluster } }"
        class="ch-jobs-tab"
        :data-active="mode === 'past'"
        role="tab"
        >Terminated</RouterLink
      >
      <span class="ch-jobs-count">
        <template v-if="loaded"><b>{{ count }}</b> job{{ count === 1 ? '' : 's' }}</template>
        <template v-else>loading…</template>
      </span>
    </div>
    <div class="ch-jobs-tools">
      <JobsPastTimeRangeSelector
        v-if="mode === 'past' && maxHours && defaultHours"
        :max-hours="maxHours"
        :default-hours="defaultHours"
        @select="emit('pastHours', $event)"
      />
      <label class="ch-jobs-search">
        <span aria-hidden="true">⌕</span>
        <input
          v-model="runtimeStore.jobs.filters.name"
          type="search"
          autocomplete="off"
          spellcheck="false"
          placeholder="Search job name or /regex/"
        />
      </label>
    </div>
  </div>
</template>
