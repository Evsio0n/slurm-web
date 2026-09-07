<!--
  Copyright (c) 2023-2024 Rackslab

  This file is part of Slurm-web.

  SPDX-License-Identifier: MIT
-->

<script setup lang="ts">
import { RouterLink } from 'vue-router'
import { Dialog, DialogPanel, TransitionChild, TransitionRoot } from '@headlessui/vue'
import {
  CalendarIcon,
  Cog6ToothIcon,
  HomeIcon,
  PlayCircleIcon,
  CpuChipIcon,
  SwatchIcon,
  XMarkIcon,
  UserGroupIcon
} from '@heroicons/vue/24/outline'

import { useRuntimeStore } from '@/stores/runtime'
import { useRuntimeConfiguration } from '@/plugins/runtimeConfiguration'

const { entry } = defineProps<{
  entry: string
}>()

const sidebarOpen = defineModel<boolean>()

const runtimeStore = useRuntimeStore()
const runtimeConfiguration = useRuntimeConfiguration()
const navigation: Array<{
  name: string
  route: string
  icon: typeof HomeIcon
  permissions: string[]
}> = [
  { name: 'Dashboard', route: 'dashboard', icon: HomeIcon, permissions: ['stats-view'] },
  {
    name: 'Jobs',
    route: 'jobs',
    icon: PlayCircleIcon,
    permissions: ['jobs-view', 'jobs-view-own', 'jobs-view-past', 'jobs-view-past-own']
  },
  { name: 'Resources', route: 'resources', icon: CpuChipIcon, permissions: ['nodes-view'] },
  { name: 'QOS', route: 'qos', icon: SwatchIcon, permissions: ['qos-view'] },
  {
    name: 'Reservations',
    route: 'reservations',
    icon: CalendarIcon,
    permissions: ['reservations-view']
  },
  {
    name: 'Accounts',
    route: 'accounts',
    icon: UserGroupIcon,
    permissions: ['associations-view']
  }
]
</script>

<template>
  <TransitionRoot as="template" :show="sidebarOpen">
    <Dialog as="div" class="relative z-50 lg:hidden" @close="sidebarOpen = false">
      <TransitionChild
        as="template"
        enter="transition-opacity ease-linear duration-300"
        enter-from="opacity-0"
        enter-to="opacity-100"
        leave="transition-opacity ease-linear duration-300"
        leave-from="opacity-100"
        leave-to="opacity-0"
      >
        <div class="fixed inset-0 bg-gray-900/80" />
      </TransitionChild>

      <div class="fixed inset-0 flex">
        <TransitionChild
          as="template"
          enter="transition ease-in-out duration-300 transform"
          enter-from="-translate-x-full"
          enter-to="translate-x-0"
          leave="transition ease-in-out duration-300 transform"
          leave-from="translate-x-0"
          leave-to="-translate-x-full"
        >
          <DialogPanel class="relative mr-16 flex w-full max-w-xs flex-1">
            <TransitionChild
              as="template"
              enter="ease-in-out duration-300"
              enter-from="opacity-0"
              enter-to="opacity-100"
              leave="ease-in-out duration-300"
              leave-from="opacity-100"
              leave-to="opacity-0"
            >
              <div class="absolute top-0 left-full flex w-16 justify-center pt-5">
                <button type="button" class="-m-2.5 p-2.5" @click="sidebarOpen = false">
                  <span class="sr-only">Close sidebar</span>
                  <XMarkIcon class="h-6 w-6 text-white" aria-hidden="true" />
                </button>
              </div>
            </TransitionChild>

            <!-- Sidebar component -->
            <div class="ch-sidebar flex grow flex-col gap-y-5 overflow-y-auto px-3 pb-4">
              <div class="ch-brand flex h-16 shrink-0 items-center gap-3 px-2">
                <span class="ch-brand-mark">S</span>
                <span><b>Slurm Web</b><small>ENGINEER CONSOLE · {{ runtimeConfiguration.version }}</small></span>
              </div>
              <nav class="flex flex-1 flex-col">
                <ul role="list" class="flex flex-1 flex-col gap-y-7">
                  <li>
                    <ul role="list" class="-mx-2 space-y-1">
                      <li v-for="item in navigation" :key="item.name">
                        <RouterLink
                          v-if="runtimeStore.hasAnyPermission(item.permissions)"
                          :to="{ name: item.route }"
                          :class="[
                            item.route == entry
                              ? 'ch-nav-active'
                              : 'ch-nav-idle',
                            'ch-nav-item group flex gap-x-3 rounded-md p-2 text-sm leading-6 font-semibold'
                          ]"
                          @click="sidebarOpen = false"
                        >
                          <component
                            :is="item.icon"
                            :class="[
                              item.route == entry
                                ? 'text-[#faff69]'
                                : 'text-gray-400 group-hover:text-gray-100',
                              'h-6 w-6 shrink-0'
                            ]"
                            aria-hidden="true"
                          />
                          {{ item.name }}
                        </RouterLink>
                      </li>
                    </ul>
                  </li>
                  <li class="mt-auto">
                    <RouterLink
                      :to="{ name: 'settings' }"
                      class="ch-nav-item ch-nav-idle group flex gap-x-3 rounded-md p-2 text-sm leading-6 font-semibold"
                    >
                      <Cog6ToothIcon
                        class="text-slurmweb-font-disabled h-6 w-6 shrink-0 group-hover:text-white"
                        aria-hidden="true"
                      />
                      Settings
                    </RouterLink>
                  </li>
                </ul>
              </nav>
            </div>
          </DialogPanel>
        </TransitionChild>
      </div>
    </Dialog>
  </TransitionRoot>

  <!-- Static sidebar for desktop -->
  <div class="hidden lg:fixed lg:inset-y-0 lg:z-50 lg:flex lg:w-60 lg:flex-col">
    <!-- Sidebar component, swap this element with another sidebar if you like -->
    <div class="ch-sidebar flex grow flex-col gap-y-5 overflow-y-auto px-3 pb-4">
      <div class="ch-brand flex h-17 shrink-0 items-center gap-3 px-2">
        <span class="ch-brand-mark">S</span>
        <span><b>Slurm Web</b><small>ENGINEER CONSOLE · {{ runtimeConfiguration.version }}</small></span>
      </div>
      <nav class="flex flex-1 flex-col">
        <ul role="list" class="flex flex-1 flex-col gap-y-7">
          <li>
            <ul role="list" class="-mx-2 space-y-1">
              <li v-for="item in navigation" :key="item.name">
                <RouterLink
                  v-if="runtimeStore.hasAnyPermission(item.permissions)"
                  :to="{ name: item.route }"
                  :class="[
                    item.route == entry
                      ? 'ch-nav-active'
                      : 'ch-nav-idle',
                    'ch-nav-item group flex gap-x-3 rounded-md p-2 text-sm leading-6 font-semibold'
                  ]"
                >
                  <component :is="item.icon" :class="['h-6 w-6 shrink-0']" aria-hidden="true" />
                  {{ item.name }}
                </RouterLink>
              </li>
            </ul>
          </li>
          <li class="mt-auto">
            <RouterLink
              :to="{ name: 'settings' }"
              class="ch-nav-item ch-nav-idle group flex gap-x-3 rounded-md p-2 text-sm leading-6 font-semibold"
            >
              <Cog6ToothIcon class="h-6 w-6 shrink-0" aria-hidden="true" />
              Settings
            </RouterLink>
          </li>
        </ul>
      </nav>
    </div>
  </div>
</template>
