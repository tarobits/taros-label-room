<script setup lang="ts">
import { locale, t } from '../shared/i18n'

import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { api, errorMessage } from '../shared/api'
import type { Batch, BatchDetail } from '../shared/types'
import { useLiveRefresh } from '../shared/live'
import PackingLabels from './PackingLabels.vue'
import MotionSurface from './MotionSurface.vue'
import { reducedMotion } from '../shared/preferences'

const props = defineProps<{ printerId: string; printerName: string }>()
defineEmits<{ close: [] }>()
const batches = ref<Batch[]>([]),
  batch = ref<BatchDetail | null>(null)
const root = ref<HTMLElement>()
const order = ref<'print' | 'requester'>('print')
const navigating = ref(false),
  detailsLoading = ref(false)
const selected = ref(''),
  page = ref(0),
  total = ref(0),
  error = ref(''),
  loading = ref(true)
const before = ref<number | null>(null)
const exhausted = ref({ previous: false, next: false })
const pages = computed(() => Math.max(1, Math.ceil(total.value / 20)))
const complete = (item: Batch) => !['printing', 'needs_review'].includes(item.state)
const completed = computed(() => batches.value.filter(complete))
const currentIndex = computed(() => completed.value.findIndex((item) => item.id === selected.value))
const canPrevious = computed(
  () =>
    !loading.value &&
    !navigating.value &&
    !exhausted.value.previous &&
    (currentIndex.value > 0 || page.value > 0),
)
const canNext = computed(
  () =>
    !loading.value &&
    !navigating.value &&
    !exhausted.value.next &&
    (currentIndex.value + 1 < completed.value.length || page.value + 1 < pages.value),
)
const ready = computed(
  () =>
    !loading.value &&
    !navigating.value &&
    !detailsLoading.value &&
    !!batch.value &&
    batch.value.id === selected.value &&
    complete(batch.value),
)
let generation = 0,
  detailGeneration = 0,
  disposed = false
onBeforeUnmount(() => {
  disposed = true
  generation++
  detailGeneration++
})
async function loadBatch() {
  const ticket = ++detailGeneration,
    id = selected.value
  if (!id) {
    batch.value = null
    detailsLoading.value = false
    return
  }
  detailsLoading.value = true
  try {
    const value = await api<BatchDetail>(`/proxy/batches/${encodeURIComponent(id)}`)
    if (disposed || ticket !== detailGeneration) return
    batch.value = value
    error.value = ''
  } catch (issue) {
    if (disposed || ticket !== detailGeneration) return
    batch.value = null
    error.value = errorMessage(issue)
  } finally {
    if (!disposed && ticket === detailGeneration) detailsLoading.value = false
  }
}
function fetchPage(index: number, boundary = index ? before.value : null) {
  const query = new URLSearchParams({
    printer_id: props.printerId,
    limit: '20',
    offset: String(index * 20),
  })
  if (boundary !== null) query.set('before', String(boundary))
  return api<{ items: Batch[]; total: number; before: number }>(`/proxy/batches/page?${query}`)
}
async function load() {
  if (navigating.value) return
  const ticket = ++generation
  try {
    const result = await fetchPage(page.value)
    if (disposed || ticket !== generation) return
    exhausted.value = { previous: false, next: false }
    batches.value = result.items
    total.value = result.total
    before.value = result.before
    error.value = ''
    if (!result.items.some((item) => item.id === selected.value && complete(item))) {
      selected.value = result.items.find(complete)?.id ?? ''
      if (!selected.value) batch.value = null
    } else await loadBatch()
  } catch (issue) {
    if (disposed || ticket !== generation) return
    batches.value = []
    batch.value = null
    detailGeneration++
    error.value = errorMessage(issue)
  } finally {
    if (!disposed && ticket === generation) loading.value = false
  }
}
watch(selected, () => {
  exhausted.value = { previous: false, next: false }
  order.value = 'print'
  batch.value = null
  void loadBatch()
})
function changePage(delta: number) {
  if (loading.value || navigating.value) return
  const target = page.value + delta
  if (target < 0 || target >= pages.value) return
  page.value = target
  selected.value = ''
  batches.value = []
  loading.value = true
  void load()
}
async function moveRun(direction: -1 | 1) {
  if (direction === -1 ? !canPrevious.value : !canNext.value) return
  const adjacent = completed.value[currentIndex.value + direction]
  if (adjacent) {
    selected.value = adjacent.id
    return
  }
  // Search across pages without replacing the visible run until a request
  // succeeds. Skip unfinished runs and preserve the pagination boundary.
  const ticket = ++generation,
    boundary = before.value
  navigating.value = true
  try {
    for (let target = page.value + direction; target >= 0; target += direction) {
      const result = await fetchPage(target, boundary)
      if (disposed || ticket !== generation) return
      const eligible = result.items.filter(complete)
      if (eligible.length) {
        page.value = target
        batches.value = result.items
        total.value = result.total
        before.value = result.before
        selected.value = (direction === 1 ? eligible[0] : eligible.at(-1))?.id ?? ''
        if (!selected.value) batch.value = null
        error.value = ''
        break
      }
      if (target === 0 || target + 1 >= Math.ceil(result.total / 20)) {
        exhausted.value[direction === 1 ? 'next' : 'previous'] = true
        error.value = ''
        break
      }
    }
  } catch (issue) {
    if (!disposed && ticket === generation) error.value = errorMessage(issue)
  } finally {
    if (!disposed && ticket === generation) navigating.value = false
  }
}
function setOrder(value: 'print' | 'requester') {
  if (ready.value) order.value = value
}
function scroll(direction: -1 | 1) {
  if (!ready.value) return
  window.scrollBy({
    top: direction * window.innerHeight * 0.75,
    behavior: reducedMotion.value ? 'instant' : 'smooth',
  })
}
watch(selected, async () => {
  await nextTick()
  if (!disposed) root.value?.scrollIntoView({ block: 'start' })
})
defineExpose({ selected, page, order, ready, canPrevious, canNext, moveRun, setOrder, scroll })
useLiveRefresh(load, 10000)
const title = (item: Batch) =>
  `${new Date(item.created_at * 1000).toLocaleString(locale.value)} · ${item.process_names?.join(', ') || t('companionPacking.batch')}`
</script>

<template>
  <section ref="root" class="station-packing" :aria-label="$t('companionPacking.batchPacking')">
    <div class="row wrap spread">
      <h2 tabindex="-1">{{ $t('companionPacking.packLabels') }} {{ printerName }}</h2>
      <button class="secondary" @click="$emit('close')">
        {{ $t('companionPacking.backToPrinters') }}
      </button>
    </div>
    <p v-if="error" class="alert danger" role="alert">{{ error }}</p>
    <p v-if="loading" class="muted" role="status">{{ $t('companionPacking.loadingBatches') }}</p>
    <div v-if="batches.length || page" class="packing-batches">
      <label
        >{{ $t('companionPacking.batch')
        }}<select v-model="selected" :disabled="loading || navigating">
          <option value="" disabled>{{ $t('companionPacking.chooseACompletedBatch') }}</option>
          <option
            v-for="item in batches"
            :key="item.id"
            :value="item.id"
            :disabled="!complete(item)"
          >
            {{ title(item) }}{{ complete(item) ? '' : $t('uiRemainder.notPacked') }}
          </option>
        </select></label
      >
      <nav v-if="pages > 1" class="row" :aria-label="$t('companionPacking.batchPages')">
        <button
          class="secondary"
          :disabled="loading || navigating || !page"
          @click="changePage(-1)"
        >
          {{ $t('common.previous') }}
        </button>
        <span>{{ page + 1 }} / {{ pages }}</span>
        <button
          class="secondary"
          :disabled="loading || navigating || page + 1 >= pages"
          @click="changePage(1)"
        >
          {{ $t('common.next') }}
        </button>
      </nav>
    </div>
    <p v-if="!loading && !error && !selected" class="empty compact">
      {{ $t('companionPacking.noCompletedBatchesOnThisPage') }}
    </p>
    <MotionSurface :identity="selected">
      <PackingLabels
        v-if="batch && complete(batch)"
        :key="batch.id"
        :batch="batch"
        v-model:order="order"
        api-prefix="/proxy"
      />
    </MotionSurface>
  </section>
</template>

<style scoped>
.station-packing {
  display: grid;
  gap: 16px;
}
.station-packing h2 {
  margin: 0;
  overflow-wrap: anywhere;
}
.packing-batches {
  display: flex;
  align-items: end;
  flex-wrap: wrap;
  gap: 16px;
}
.packing-batches label {
  flex: 1 1 320px;
  min-width: 0;
}
.packing-batches select {
  width: 100%;
}
.packing-batches nav {
  flex-wrap: wrap;
}
.station-packing button {
  min-height: 44px;
}
</style>
