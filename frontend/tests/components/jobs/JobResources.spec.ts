import { describe, test, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import JobResources from '@/components/jobs/JobResources.vue'
import jobs from '../../assets/jobs.json'

describe('JobResources.vue', () => {
  test('job with gpus', () => {
    const job = { ...jobs[0] }
    job.node_count.number = 4
    job.cpus.number = 16
    job.gres_detail = ['gpu:h100:2(IDX:2-3)']
    const wrapper = mount(JobResources, {
      props: {
        job: job
      }
    })
    const items = wrapper.findAll('.ch-resources > span')
    expect(items.length).toBe(3)
    expect(items[0].text()).toBe('4 nodes')
    expect(items[1].text()).toBe('16 CPU')
    expect(items[2].text()).toBe('2 GPU')
  })
  test('job without gpus', () => {
    const job = { ...jobs[0] }
    job.node_count.number = 2
    job.cpus.number = 8
    job.gres_detail = []
    const wrapper = mount(JobResources, {
      props: {
        job: job
      }
    })
    const items = wrapper.findAll('.ch-resources > span')
    expect(items.length).toBe(3)
    expect(items[0].text()).toBe('2 nodes')
    expect(items[1].text()).toBe('8 CPU')
    expect(items[2].text()).toBe('no GPU')
    expect(items[2].classes()).toContain('ch-resources-none')
  })
  test('job with gpus unreliable', () => {
    const job = { ...jobs[0] }
    job.node_count.number = 4
    job.cpus.number = 16
    job.gres_detail = []
    job.tres_per_socket = 'gres/gpu:2'
    job.sockets_per_node.set = true
    job.sockets_per_node.number = 2
    const wrapper = mount(JobResources, {
      props: {
        job: job
      }
    })
    const items = wrapper.findAll('.ch-resources > span')
    expect(items.length).toBe(3)
    expect(items[0].text()).toBe('4 nodes')
    expect(items[1].text()).toBe('16 CPU')
    expect(items[2].text()).toBe('16 GPU~')
    expect(items[2].attributes('title')).toContain('estimated')
  })
})
