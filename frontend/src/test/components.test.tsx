import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { RecommendationCard, ReadinessDrawer } from '../components'
import type { Progress, Recommendation } from '../api'

const recommendation: Recommendation = {
  event_id:'EV_TEST',title:'Architecture Lab',type:'workshop',format:'offline',duration_hours:2,
  next_session:'2026-10-02',match_label:'Strong match',score:14,
  expected_effect:[{skill_id:'SK_DESIGN',name:'System Design',from_level:2,to_level:3,target_level:4,critical:true}],
  factors:[{code:'critical_gap_closure',contribution:12,detail:'Closes one critical level.'},{code:'goal_relevance',contribution:2.5,detail:'Targets the chosen role.'},{code:'effort',contribution:-.08,detail:'Two hours.'}],
  explanation:'System Design is a critical target gap and this workshop can move the estimate from L2 to L3.',
}

const progress: Progress = {
  employee_id:'E_TEST',assessed_at:'2026-05-01',target:{role:'Backend Engineer',grade:'Senior',source:'career_goal',is_lateral:false},
  readiness:{status:'critical_gaps',requirements_met:0,requirements_total:1,critical_requirements_met:0,critical_requirements_total:1,readiness_pct:50,formula:'round(100 × Σ min(effective_level, target_level) / Σ target_level)',contributions:[{skill_id:'SK_DESIGN',name:'System Design',effective_level:2,target_level:4,credited_level:2}]},
  skills:[],gaps:[],critical_gaps:[],completed_after_review:[],
}

describe('explainable UI',()=>{
  it('renders a recommendation with effect and factor entry point',()=>{render(<RecommendationCard item={recommendation} language="en" onWhy={vi.fn()}/>);expect(screen.getByText('Architecture Lab')).toBeInTheDocument();expect(screen.getByText(/System Design · L2 → L3/)).toBeInTheDocument();expect(screen.getByRole('button',{name:'Why this?'})).toBeInTheDocument()})
  it('shows the exact readiness formula and skill math',()=>{render(<ReadinessDrawer progress={progress} language="en" onClose={vi.fn()}/>);expect(screen.getByText(/round\(100 × 2 ÷ 4\)/)).toBeInTheDocument();expect(screen.getByText('2 / 4')).toBeInTheDocument()})
  it('opens the explanation action',()=>{const onWhy=vi.fn();render(<RecommendationCard item={recommendation} language="en" onWhy={onWhy}/>);fireEvent.click(screen.getByRole('button',{name:'Why this?'}));expect(onWhy).toHaveBeenCalledOnce()})
})
