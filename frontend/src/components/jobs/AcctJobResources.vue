<!--
  Copyright (c) 2025 Rackslab

  This file is part of Slurm-web.

  SPDX-License-Identifier: MIT
-->

<script setup lang="ts">
import type { SlurmAcctJob } from '@/composables/gateway/slurm/types'
import {
  acctJobAllocatedGPU,
  acctJobCPUs,
  acctJobResources
} from '@/composables/gateway/slurm/acctJob'

const { job } = defineProps<{ job: SlurmAcctJob }>()
const jobResources = acctJobResources(job)
const gpu = acctJobAllocatedGPU(job)
const nodes = jobResources.node >= 0 ? jobResources.node : 0
const jobCpus = acctJobCPUs(job)
</script>
<template>
  <span class="ch-resources">
    <span><b>{{ nodes }}</b> node{{ nodes > 1 ? 's' : '' }}</span>
    <span><b>{{ jobCpus }}</b> CPU</span>
    <span v-if="gpu > 0" class="ch-resources-gpu"><b>{{ gpu }}</b> GPU</span>
    <span v-else class="ch-resources-none">no GPU</span>
  </span>
</template>
