export type Role = 'employee' | 'hr'

export interface Session { token: string; role: Role; employeeId?: string }
export interface PickerEmployee { employee_id: string; full_name: string; role: string; grade: string }
export interface Employee extends PickerEmployee {
  department: string; preferred_language: 'en' | 'ru' | 'kk'; work_format: string;
  hire_date: string;
  career_goal: { target_role: string; target_grade: string } | null;
  activity_summary: { history_records: number; completed: number; completed_event_ids: string[] }
}
export interface SkillProgress {
  skill_id: string; name: string; category: string; assessed_level: number;
  post_review_development: number; effective_level: number; target_level: number;
  gap: number; critical: boolean; evidence_event_ids: string[]
}
export interface Progress {
  employee_id: string; assessed_at: string;
  target: { role: string; grade: string; source: string; is_lateral: boolean };
  readiness: { status: string; requirements_met: number; requirements_total: number; critical_requirements_met: number; critical_requirements_total: number; readiness_pct: number; formula: string; contributions: Array<{skill_id:string; name:string; effective_level:number; target_level:number; credited_level:number}> };
  skills: SkillProgress[]; gaps: SkillProgress[]; critical_gaps: SkillProgress[]; completed_after_review: string[]
}
export interface Factor { code: string; contribution: number; detail: string }
export interface Recommendation {
  event_id: string; title: string; type: string; format: string; duration_hours: number;
  next_session: string | null; match_label: string; score: number;
  expected_effect: Array<{skill_id:string; name:string; from_level:number; to_level:number; target_level:number; critical:boolean}>;
  factors: Factor[]; explanation: string
}
export interface Recommendations {
  employee_id: string; as_of_date: string; status: string; summary: string;
  target: Progress['target']; recommendations: Recommendation[];
  required: Array<{event_id:string; title:string; status:string; due_date:string|null}>;
  blocked_gaps: Array<{skill_id:string; name:string; effective_level:number; target_level:number; critical:boolean; suggestion:string}>;
  unclosed_gaps: Array<{skill_id:string; name:string; effective_level:number; target_level:number; gap:number; critical:boolean}>
}
export interface Journey { employee_id:string; message:string; progress_since_review:Array<{skill_id:string;name:string;assessed_level:number;effective_level:number;gain:number;evidence_event_ids:string[]}>; monthly_activity:Array<{month:string;records:number;completed:number;in_progress:number;friction:number}> }
export interface ActivityDiff { record_id:string; event_id:string; status:string; skills_changed:Array<{skill_id:string;name:string;from_level:number;to_level:number}>; readiness:{from:number;to:number}; recommendations_before:string[]; recommendations_after:string[]; progress_note:string }

const API = import.meta.env.VITE_API_URL || '/api'

export async function request<T>(path: string, options: RequestInit = {}, token?: string): Promise<T> {
  const headers = new Headers(options.headers)
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  const response = await fetch(`${API}${path}`, { ...options, headers })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.detail || body.errors?.[0]?.message || `Request failed (${response.status})`)
  return body as T
}

export const api = {
  picker: (search='') => request<{items:PickerEmployee[]}>(`/auth/demo-employees?limit=200&search=${encodeURIComponent(search)}`),
  login: (role: Role, employeeId?: string) => request<{access_token:string;role:Role;employee_id?:string}>('/auth/demo-login', {method:'POST', body:JSON.stringify({role, employee_id:employeeId})}),
  employee: (id:string, token:string) => request<Employee>(`/employees/${id}`, {}, token),
  progress: (id:string, token:string) => request<Progress>(`/employees/${id}/progress`, {}, token),
  recommendations: (id:string, token:string) => request<Recommendations>(`/employees/${id}/recommendations`, {}, token),
  journey: (id:string, token:string) => request<Journey>(`/employees/${id}/journey`, {}, token),
  activity: (id:string,eventId:string,status:'completed'|'in_progress',token:string) => request<ActivityDiff>(`/employees/${id}/activities`, {method:'POST',body:JSON.stringify({event_id:eventId,status,source:'self_report'})}, token),
  hr: <T,>(path:string, token:string) => request<T>(`/hr/${path}`, {}, token),
  importData: (files:File[], dryRun:boolean, token:string) => { const form=new FormData(); files.forEach(file=>form.append('files',file)); return request<{added_employees:string[];added_history:string[];warnings:string[];errors:Array<{file:string;row?:number;id?:string;message:string}>}>(`/import${dryRun?'/dry-run':''}`,{method:'POST',body:form},token) },
}
