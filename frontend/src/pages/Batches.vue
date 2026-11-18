<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'
const rows = ref([])
const err = ref('')
async function load() {
  try { rows.value = await api.batches() } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
<template>
  <div class="panel">
    <p class="msg err" v-if="err">{{ err }}</p>
    <table>
      <thead><tr><th>批次</th><th>业务日</th><th>状态</th><th>笔数</th><th>合计</th><th>钉扎时刻</th><th>指纹</th></tr></thead>
      <tbody>
        <tr v-for="b in rows" :key="b.id">
          <td>{{ b.id }}</td>
          <td>{{ b.business_day }}</td>
          <td><span class="tag" :class="{ ok: b.fingerprint_ok && b.header_ok, warn: !(b.fingerprint_ok && b.header_ok) }">{{ b.status }}</span></td>
          <td>{{ b.fact_count }}</td>
          <td>{{ b.total_amount }}</td>
          <td>{{ b.sealed_at }}</td>
          <td>
            <span class="tag" :class="{ ok: b.fingerprint_ok, warn: !b.fingerprint_ok }">
              {{ b.fingerprint_ok ? '自洽' : '分叉' }}
            </span>
            <span class="tag" :class="{ ok: b.header_ok, warn: !b.header_ok }">
              {{ b.header_ok ? '批头一致' : '批头漂移' }}
            </span>
          </td>
        </tr>
        <tr v-for="b in rows.filter(x => x.diverged_facts && x.diverged_facts.length)" :key="'d-' + b.id">
          <td colspan="7">
            <p class="msg err">批次 #{{ b.id }} 已钉行被改写：
              <template v-for="d in b.diverged_facts" :key="d.fact_id">
                事实 #{{ d.fact_id }} 钉时 {{ d.pinned?.amount }} → 现值 {{ d.current?.amount }}；
              </template>
            </p>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
