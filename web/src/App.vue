<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import {
  Activity,
  Check,
  CheckCircle2,
  ChevronRight,
  CircleDot,
  FileText,
  FlaskConical,
  Play,
  RefreshCw,
  Send,
  X,
} from '@lucide/vue'
import { api, type ArtifactBundle, type ScenarioProposal, type ScenarioReview, type VerificationState } from './api'

const verificationId = ref('')
const state = ref<VerificationState>()
const review = ref<ScenarioReview>()
const artifacts = ref<ArtifactBundle>()
const busy = ref(false)
const error = ref('')
const message = ref('')
const approvalReason = ref('')
let pollTimer: number | undefined

const form = ref({
  workflowStepId: 'integration-test-01',
  requirementPath: 'C:/dev/ai26/resource/input/pot-spec-v6.pdf',
  interfacePath: 'C:/dev/ai26/ai26-pot-test-env/doc/api',
  productPath: 'C:/dev/ai26/ai26-pot-application',
  environmentId: 'ai-gen-test',
  maxRepairRounds: 2,
})

const steps = [
  { id: 1, label: 'シナリオ作成', icon: FileText },
  { id: 2, label: 'テストケース作成', icon: FlaskConical },
  { id: 3, label: 'テスト実行', icon: Play },
  { id: 4, label: '結果承認', icon: CheckCircle2 },
]

const currentStep = computed(() => {
  const status = state.value?.status
  if (!status || ['accepted', 'generating_scenarios', 'reviewing_scenarios'].includes(status)) return 1
  if (['generating_testcases', 'awaiting_approval'].includes(status)) return 2
  if (['uploading_product', 'executing_tests', 'analyzing_results', 'repairing_testcases'].includes(status)) return 3
  return 4
})

const progress = computed(() => {
  const value = state.value?.progress
  return value?.total ? Math.round((value.completed / value.total) * 100) : 0
})

const pendingProposals = computed(() => review.value?.proposals.filter((item) => item.status === 'pending') ?? [])
const terminal = computed(() => state.value?.status.startsWith('completed'))

async function run(action: () => Promise<void>) {
  busy.value = true
  error.value = ''
  try {
    await action()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : String(reason)
  } finally {
    busy.value = false
  }
}

async function createVerification() {
  await run(async () => {
    const response = await api.create({
      workflow_step_id: form.value.workflowStepId,
      source_documents: [{
        type: 'requirement_spec',
        title: '要求仕様書',
        artifact: { path: form.value.requirementPath },
      }],
      interface_spec: { path: form.value.interfacePath },
      product: { path: form.value.productPath },
      test_environment_id: form.value.environmentId,
      idempotency_key: `${form.value.workflowStepId}-${Date.now()}`,
      review_scenarios: true,
      require_result_approval: true,
      max_repair_rounds: form.value.maxRepairRounds,
    })
    verificationId.value = response.verification_id
    await refresh()
    startPolling()
  })
}

async function refresh() {
  if (!verificationId.value) return
  state.value = await api.state(verificationId.value)
  if (state.value.status === 'reviewing_scenarios') review.value = await api.scenarios(verificationId.value)
  if (['awaiting_result_approval', 'completed', 'completed_ok', 'completed_ng'].includes(state.value.status)) {
    artifacts.value = await api.artifacts(verificationId.value)
  }
  if (terminal.value || state.value.status === 'error') stopPolling()
}

function startPolling() {
  stopPolling()
  pollTimer = window.setInterval(() => run(refresh), 1500)
}

function stopPolling() {
  if (pollTimer) window.clearInterval(pollTimer)
  pollTimer = undefined
}

async function sendMessage() {
  const content = message.value.trim()
  if (!content || !review.value) return
  await run(async () => {
    await api.refine(verificationId.value, content, review.value!.revision)
    message.value = ''
    review.value = await api.scenarios(verificationId.value)
  })
}

async function decideProposal(proposal: ScenarioProposal, action: 'accept' | 'reject') {
  await run(async () => {
    await api.proposal(verificationId.value, proposal.proposal_id, action)
    review.value = await api.scenarios(verificationId.value)
  })
}

async function approveScenarios() {
  await run(async () => {
    await api.approveScenarios(verificationId.value)
    review.value = undefined
    await refresh()
    startPolling()
  })
}

async function approveResult(action: string) {
  await run(async () => {
    await api.approveResult(verificationId.value, action, approvalReason.value)
    approvalReason.value = ''
    await refresh()
    if (action === 'restart_from_scenarios') startPolling()
  })
}

onUnmounted(stopPolling)
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="brand-mark">CT</div>
      <div>
        <strong>Co-Test Workspace</strong>
        <span>結合検証オーケストレーター</span>
      </div>
      <div class="topbar-status">
        <CircleDot :size="15" />
        {{ state?.status ?? '準備完了' }}
      </div>
    </header>

    <aside class="rail">
      <div class="rail-heading">検証工程</div>
      <button v-for="step in steps" :key="step.id" class="step" :class="{ active: currentStep === step.id, done: currentStep > step.id }">
        <span class="step-index"><Check v-if="currentStep > step.id" :size="15" /><component :is="step.icon" v-else :size="17" /></span>
        <span><small>STEP {{ step.id }}</small>{{ step.label }}</span>
        <ChevronRight :size="16" />
      </button>
      <div v-if="verificationId" class="job-meta">
        <small>VERIFICATION ID</small>
        <code>{{ verificationId }}</code>
        <button class="icon-button" title="状態を更新" @click="run(refresh)"><RefreshCw :size="16" /></button>
      </div>
    </aside>

    <main class="workspace">
      <div v-if="error" class="error-banner">{{ error }}</div>

      <section v-if="!state" class="setup-panel">
        <div class="section-heading">
          <span class="eyebrow">NEW VERIFICATION</span>
          <h1>結合検証を開始</h1>
          <p>入力資料と実行環境を指定し、AIと共同で検証を進めます。</p>
        </div>
        <form class="setup-form" @submit.prevent="createVerification">
          <label>工程ID<input v-model="form.workflowStepId" required /></label>
          <label class="wide">要求仕様書<input v-model="form.requirementPath" required /></label>
          <label class="wide">シミュレーションI/F<input v-model="form.interfacePath" required /></label>
          <label class="wide">テスト対象ソフトウェア<input v-model="form.productPath" required /></label>
          <label>テスト環境<select v-model="form.environmentId"><option value="ai-gen-test">AI Gen Test MCP</option><option value="local">Local command runner</option></select></label>
          <label>自動修復上限<input v-model.number="form.maxRepairRounds" type="number" min="0" max="10" /></label>
          <button class="primary wide" :disabled="busy"><Play :size="17" />検証を開始</button>
        </form>
      </section>

      <template v-else>
        <section class="status-strip">
          <div><span>現在の工程</span><strong>{{ steps[currentStep - 1]?.label }}</strong></div>
          <div><span>進捗</span><strong>{{ state.progress.completed }} / {{ state.progress.total }}</strong></div>
          <div><span>Agent round</span><strong>{{ state.agent_round }} / {{ state.max_rounds }}</strong></div>
          <div><span>Repair</span><strong>{{ state.repair_count }}</strong></div>
          <div class="progress-track"><i :style="{ width: `${progress}%` }"></i></div>
        </section>

        <section v-if="state.status === 'reviewing_scenarios' && review" class="review-layout">
          <div class="scenario-pane">
            <div class="pane-heading"><div><span class="eyebrow">REVISION {{ review.revision }}</span><h2>テストシナリオ</h2></div><span class="count">{{ review.scenarios.length }}件</span></div>
            <article v-for="(scenario, index) in review.scenarios" :key="index" class="scenario-row">
              <span class="number">{{ String(index + 1).padStart(2, '0') }}</span>
              <div><h3>{{ scenario.summary }}</h3><ul><li v-for="viewpoint in scenario.viewpoints" :key="viewpoint">{{ viewpoint }}</li></ul></div>
            </article>
            <button class="primary approve" :disabled="busy || pendingProposals.length > 0" @click="approveScenarios"><CheckCircle2 :size="18" />シナリオを確定</button>
          </div>

          <div class="conversation-pane">
            <div class="pane-heading"><div><span class="eyebrow">COLLABORATION</span><h2>AIレビュー</h2></div></div>
            <div class="messages">
              <div v-if="review.messages.length === 0" class="empty-message">修正したい観点を入力してください。</div>
              <div v-for="item in review.messages" :key="item.created_at" class="message" :class="item.role">{{ item.content }}</div>
            </div>
            <article v-for="proposal in pendingProposals" :key="proposal.proposal_id" class="proposal">
              <strong>変更提案</strong><p>{{ proposal.reason }}</p>
              <div v-for="change in proposal.changes" :key="change.index" class="change"><span :class="change.operation">{{ change.operation }}</span><b>#{{ change.index + 1 }}</b>{{ change.after?.summary ?? change.before?.summary }}</div>
              <div class="proposal-actions"><button title="提案を却下" @click="decideProposal(proposal, 'reject')"><X :size="17" />却下</button><button class="accept" title="提案を採用" @click="decideProposal(proposal, 'accept')"><Check :size="17" />採用</button></div>
            </article>
            <form class="composer" @submit.prevent="sendMessage"><textarea v-model="message" placeholder="例: 異常系の観点を追加してください" rows="3"></textarea><button class="icon-button send" title="メッセージを送信" :disabled="busy"><Send :size="18" /></button></form>
          </div>
        </section>

        <section v-else-if="state.status === 'awaiting_result_approval'" class="result-panel">
          <div class="section-heading"><span class="eyebrow">HUMAN DECISION</span><h1>テスト結果を承認</h1><p>AIの分析と実行履歴を確認し、検証の完了方法を選択します。</p></div>
          <div v-if="artifacts?.result_analyses.length" class="analysis">
            <Activity :size="21" /><div><strong>{{ artifacts.result_analyses.at(-1)?.summary }}</strong><p>{{ artifacts.result_analyses.at(-1)?.recommendation }}</p></div><span>{{ Math.round((artifacts.result_analyses.at(-1)?.confidence ?? 0) * 100) }}%</span>
          </div>
          <div class="result-metrics"><div><span>テストケース</span><strong>{{ artifacts?.testcases.length ?? 0 }}</strong></div><div><span>実行ラウンド</span><strong>{{ artifacts?.execution_log_paths.length ?? 0 }}</strong></div><div><span>自動修復</span><strong>{{ state.repair_count }}</strong></div></div>
          <label class="reason">判断理由<textarea v-model="approvalReason" rows="3" placeholder="実行判定と異なる判断をする場合は必須です"></textarea></label>
          <div class="decision-row"><button @click="approveResult('restart_from_scenarios')"><RefreshCw :size="18" />シナリオからやり直す</button><button class="danger" @click="approveResult('complete_ng')"><X :size="18" />NGで完了</button><button class="primary" @click="approveResult('complete_ok')"><Check :size="18" />OKで完了</button></div>
        </section>

        <section v-else class="running-panel">
          <Activity :size="38" class="pulse" /><span class="eyebrow">PROCESSING</span><h1>{{ terminal ? '検証が完了しました' : '検証を進めています' }}</h1><p>{{ state.remote_phase || state.status }}</p>
          <div class="large-progress"><i :style="{ width: `${progress}%` }"></i></div>
          <button v-if="terminal" class="secondary" @click="state = undefined; verificationId = ''">新しい検証を開始</button>
        </section>
      </template>
    </main>
  </div>
</template>
