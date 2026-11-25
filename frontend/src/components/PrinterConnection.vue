<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { api, errorMessage } from '../shared/api'
import { t } from '../shared/i18n'
import ExpandableContent from './ExpandableContent.vue'

const props = withDefaults(defineProps<{ endpoint: string; canPair?: boolean }>(), {
  canPair: true,
})
const emit = defineEmits<{ busy: [value: boolean] }>()
const transport = defineModel<'usb' | 'bluetooth'>('transport', { default: 'usb' })
const serial = defineModel<string | null>('serial', { default: null })
const address = defineModel<string | null>('address', { default: null })
type Device = { address: string; name: string; paired: boolean }
const devices = ref<Device[]>([])
const busy = ref(false)
const error = ref('')
watch(busy, (value) => emit('busy', value), { flush: 'sync', immediate: true })
onBeforeUnmount(() => {
  generation++
  emit('busy', false)
})
const selector = ref<HTMLSelectElement>()
const chosen = computed(() => devices.value.find((device) => device.address === address.value))
let generation = 0
async function load(scan = false) {
  if (busy.value || !props.canPair) return
  const ticket = ++generation
  busy.value = true
  error.value = ''
  try {
    const found = await api<Device[]>(
      `${props.endpoint}${scan ? '/scan' : ''}`,
      scan ? 'POST' : 'GET',
      undefined,
      {},
      45000,
    )
    if (ticket === generation) devices.value = found
  } catch (issue) {
    if (ticket === generation) error.value = errorMessage(issue)
  } finally {
    if (ticket === generation) busy.value = false
  }
}
async function pair() {
  if (busy.value || !chosen.value || chosen.value.paired) return
  busy.value = true
  error.value = ''
  const selected = address.value
  try {
    const result = await api<Device>(
      `${props.endpoint}/pair`,
      'POST',
      { address: selected },
      {},
      45000,
    )
    devices.value = devices.value.map((device) => (device.address === selected ? result : device))
  } catch (issue) {
    error.value = errorMessage(issue)
  } finally {
    busy.value = false
  }
}
watch(
  transport,
  (value) => {
    if (value === 'bluetooth') void load()
  },
  { immediate: true },
)
watch(
  [chosen, transport, busy],
  async () => {
    await nextTick()
    selector.value?.setCustomValidity(
      transport.value === 'bluetooth' && (busy.value || (chosen.value && !chosen.value.paired))
        ? t('bluetooth.pairFirst')
        : '',
    )
  },
  { immediate: true },
)
</script>

<template>
  <div class="form-stack printer-connection">
    <label
      >{{ $t('bluetooth.connection') }}
      <select v-model="transport" :disabled="busy">
        <option value="usb">USB</option>
        <option value="bluetooth">Bluetooth</option>
      </select>
    </label>
    <ExpandableContent :open="transport === 'usb'">
      <label
        >{{ $t('common.usbSerialNumber')
        }}<input v-model="serial" maxlength="128" :disabled="transport !== 'usb'"
      /></label>
    </ExpandableContent>
    <ExpandableContent :open="transport === 'bluetooth'">
      <div class="form-stack" :aria-busy="busy">
        <label v-if="canPair"
          >{{ $t('bluetooth.printer') }}
          <select
            ref="selector"
            v-model="address"
            :required="transport === 'bluetooth'"
            :disabled="busy || transport !== 'bluetooth'"
          >
            <option :value="null">{{ $t('bluetooth.choosePrinter') }}</option>
            <option v-if="address && !chosen" :value="address">{{ address }}</option>
            <option v-for="device in devices" :key="device.address" :value="device.address">
              {{ device.name }}
            </option>
          </select>
        </label>
        <label v-else
          >{{ $t('bluetooth.address')
          }}<input
            v-model="address"
            maxlength="17"
            pattern="([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}"
            :required="transport === 'bluetooth'"
            :disabled="transport !== 'bluetooth'"
        /></label>
        <p v-if="chosen" class="muted">
          {{ chosen.address
          }}<template v-if="chosen.paired"> · {{ $t('bluetooth.paired') }}</template>
        </p>
        <div v-if="canPair" class="row wrap">
          <button type="button" class="secondary" :disabled="busy" @click="load(true)">
            {{ $t('bluetooth.scan') }}
          </button>
          <button
            v-if="chosen && !chosen.paired"
            type="button"
            class="primary"
            :disabled="busy"
            @click="pair"
          >
            {{ $t('bluetooth.pair') }}
          </button>
          <span v-if="busy" role="status" class="muted">{{ $t('bluetooth.working') }}</span>
        </div>
        <p v-if="error" role="alert" class="alert danger">{{ error }}</p>
      </div>
    </ExpandableContent>
  </div>
</template>
