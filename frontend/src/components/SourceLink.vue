<script setup lang="ts">
import { onBeforeUnmount } from 'vue'
import licenseText from '../../../LICENSE?raw'

const sourceUrl = 'https://github.com/tarobits/taros-label-room'
const licenseUrl = URL.createObjectURL(
  new Blob([licenseText], { type: 'text/plain;charset=utf-8' }),
)
onBeforeUnmount(() => URL.revokeObjectURL(licenseUrl))
</script>

<template>
  <span class="source-links">
    <a :href="sourceUrl" target="_blank" rel="noopener noreferrer">{{ $t('common.source') }}</a>
    <a
      :href="licenseUrl"
      download="PolyForm-Noncommercial-1.0.0.txt"
      :aria-label="$t('sourceLink.downloadLicense')"
      >{{ $t('sourceLink.licenseName') }}</a
    >
  </span>
</template>

<style scoped>
.source-links {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-wrap: wrap;
  gap: 10px;
}
.source-links a {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
}
</style>
