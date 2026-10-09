export type VerificationStatus =
  | 'accepted'
  | 'generating_scenarios'
  | 'reviewing_scenarios'
  | 'generating_testcases'
  | 'awaiting_approval'
  | 'uploading_product'
  | 'executing_tests'
  | 'analyzing_results'
  | 'repairing_testcases'
  | 'awaiting_result_approval'
  | 'completed'
  | 'completed_ok'
  | 'completed_ng'
  | 'error'
  | 'canceled'

export interface VerificationState {
  verification_id: string
  status: VerificationStatus
  workflow_step_id: string
  progress: { total: number; completed: number }
  remote_phase?: string
  agent_round: number
  max_rounds: number
  repair_count: number
  error?: string
}

export interface ScenarioChange {
  operation: 'add' | 'update' | 'remove'
  index: number
  before?: Scenario
  after?: Scenario
}

export interface Scenario {
  summary: string
  viewpoints: string[]
}

export interface ScenarioProposal {
  proposal_id: string
  reply: string
  reason: string
  status: 'pending' | 'accepted' | 'rejected'
  changes: ScenarioChange[]
}

export interface ScenarioReview {
  verification_id: string
  revision: number
  scenarios: Scenario[]
  messages: Array<{ role: string; content: string; created_at: string }>
  proposals: ScenarioProposal[]
}

export interface ArtifactBundle {
  testcases: Array<Record<string, unknown>>
  execution_log_paths: string[]
  result_analyses: Array<{
    classification: string
    summary: string
    facts: string[]
    inferences: string[]
    confidence: number
    recommendation: string
  }>
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(error.detail ?? 'API request failed')
  }
  return response.json() as Promise<T>
}

export const api = {
  create(payload: Record<string, unknown>) {
    return request<{ verification_id: string }>('/verifications', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  },
  state(id: string) {
    return request<VerificationState>(`/verifications/${id}`)
  },
  scenarios(id: string) {
    return request<ScenarioReview>(`/verifications/${id}/scenarios`)
  },
  refine(id: string, message: string, baseRevision: number) {
    return request<ScenarioProposal>(`/verifications/${id}/scenarios/messages`, {
      method: 'POST',
      body: JSON.stringify({ message, base_revision: baseRevision }),
    })
  },
  proposal(id: string, proposalId: string, action: 'accept' | 'reject') {
    return request<ScenarioReview | ScenarioProposal>(
      `/verifications/${id}/scenarios/proposals/${proposalId}/${action}`,
      { method: 'POST' },
    )
  },
  approveScenarios(id: string) {
    return request(`/verifications/${id}/scenarios/approve`, { method: 'POST' })
  },
  approveResult(id: string, action: string, reason: string) {
    return request(`/verifications/${id}/result-approval`, {
      method: 'POST',
      body: JSON.stringify({ action, reason: reason || null }),
    })
  },
  artifacts(id: string) {
    return request<ArtifactBundle>(`/verifications/${id}/artifacts`)
  },
}
