<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const d = ref(null)
const err = ref('')
async function load() {
  err.value = ''
  try { d.value = await api.dashboard() } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
<template>
  <div class="panel">
    <div class="grid" v-if="d">
      <div class="stat"><div class="k">业务日</div><div class="v">{{ d.business_day }}</div></div>
      <div class="stat"><div class="k">今日事件</div><div class="v">{{ d.event_count }}</div></div>
      <div class="stat"><div class="k">核销事实</div><div class="v ticket">{{ d.fact_count }}</div></div>
      <div class="stat"><div class="k">待钉 open</div><div class="v">{{ d.open_fact_count }}</div></div>
      <div class="stat"><div class="k">今日核销额</div><div class="v ticket">{{ d.redeem_total_today }}</div></div>
      <div class="stat"><div class="k">日已钉扎</div><div class="v">{{ d.day_sealed ? '是' : '否' }}</div></div>
      <div class="stat"><div class="k">批指纹</div><div class="v" :class="{ ticket: d.day_sealed && !d.fingerprint_ok }">{{ !d.day_sealed ? '未钉' : (d.fingerprint_ok ? '自洽' : '分叉') }}</div></div>
      <div class="stat"><div class="k">互证</div><div class="v" :class="{ ticket: !d.recon_ok }">{{ d.recon_ok ? "一致" : "分叉" }}</div></div>
    </div>
    <p class="msg" v-if="!err">票根洋红标出核销侧数字；钉扎后禁止再写入该日核销。</p>
    <p class="msg err" v-if="d && d.day_sealed && !d.fingerprint_ok">已钉行或批指纹被静默改写：互证判分叉，禁止按正常已钉处理。</p>
    <p class="msg err" v-if="err">{{ err }}</p>
  </div>
</template>
