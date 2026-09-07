<!--
  Copyright (c) 2025 Rackslab

  This file is part of Slurm-web.

  SPDX-License-Identifier: MIT
-->

<script setup lang="ts">
import type { SlurmJob } from '@/composables/gateway/slurm/types'
import { jobResourcesGPU } from '@/composables/gateway/slurm/job'

const { job } = defineProps<{ job: SlurmJob }>()
const gpu = jobResourcesGPU(job)
const nodes = job.node_count?.set ? job.node_count.number : 0
const cpus = job.cpus?.set ? job.cpus.number : 0
</script>
<template>
  <span class="ch-resources">
    <span :title="`${nodes} node${nodes > 1 ? 's' : ''}`"><b>{{ nodes }}</b> node{{ nodes > 1 ? 's' : '' }}</span>
    <span><b>{{ cpus }}</b> CPU</span>
    <span v-if="gpu.count" class="ch-resources-gpu" :title="gpu.reliable ? '' : 'estimated from per-node request'"><b>{{ gpu.count }}</b> GPU<template v-if="!gpu.reliable">~</template></span>
    <span v-else class="ch-resources-none">no GPU</span>
  </span>
</template>
