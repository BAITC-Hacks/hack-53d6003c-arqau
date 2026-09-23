import { NavLink } from 'react-router-dom'
import type { Progress, Recommendation } from './api'
import { localizeLabel, t, type Language } from './i18n'

export function BrandMark() { return <span className="brand-mark" aria-hidden="true"><i /></span> }
export function Initials({name}:{name:string}) { return <>{name.split(/\s+/).map(x=>x[0]).slice(0,2).join('').toUpperCase()}</> }

export function BottomNav({language}:{language:Language}) {
  return <nav className="bottom-nav" aria-label="Employee navigation">
    {[[ '/', '⌂', t(language,'home') ],['/growth','◇',t(language,'growth')],['/path','↟',t(language,'path')],['/events','□',t(language,'events')],['/ai','✦',t(language,'ai')]].map(([to,icon,label])=><NavLink key={to} to={to} end={to==='/' } className={({isActive})=>`${isActive?'active ':''}${to==='/ai'?'ai-link':''}`}><span>{icon}</span>{label}</NavLink>)}
  </nav>
}

export function RecommendationCard({item,language,onWhy,onAdd,onComplete,onDecline,compact=false}:{item:Recommendation;language:Language;onWhy:()=>void;onAdd?:()=>void;onComplete?:()=>void;onDecline?:()=>void;compact?:boolean}) {
  const effect=item.expected_effect[0]
  return <article className={`card recommendation-card ${compact?'compact':''}`} data-testid="recommendation-card">
    <div className="card-row"><span className={`match ${item.match_label==='Strong match'?'strong':''}`}>{localizeLabel(language,item.match_label)}</span><span className="muted small">{item.next_session||t(language,'flexible')}</span></div>
    <div><h3>{item.title}</h3><p className="muted small">{localizeLabel(language,item.type)} · {item.duration_hours}h · {localizeLabel(language,item.format)}</p></div>
    {effect&&<div className="effect-pill">{effect.name} · L{effect.from_level} → L{effect.to_level}{effect.critical?` · ${t(language,'criticalLabel')}`:''}</div>}
    <div className="actions">
      {onAdd&&<button className="primary" onClick={onAdd}>{t(language,'add')}</button>}
      {onComplete&&<button className="primary" onClick={onComplete}>{t(language,'complete')}</button>}
      <button className="secondary ai-text" onClick={onWhy}>{t(language,'why')}</button>
      {onDecline&&<button className="quiet" onClick={onDecline}>{t(language,'notForMe')}</button>}
    </div>
  </article>
}

export function ReadinessDrawer({progress,onClose,language}:{progress:Progress;onClose:()=>void;language:Language}) {
  const numerator=progress.readiness.contributions.reduce((sum,x)=>sum+x.credited_level,0)
  const denominator=progress.readiness.contributions.reduce((sum,x)=>sum+x.target_level,0)
  return <div className="overlay" role="presentation" onMouseDown={onClose}><section className="drawer" role="dialog" aria-modal="true" aria-label="Readiness calculation" onMouseDown={e=>e.stopPropagation()} data-testid="readiness-drawer">
    <div className="sheet-handle"/><div className="card-row"><div><span className="overline">{progress.target.grade} {t(language,'readiness')}</span><h2>{progress.readiness.readiness_pct}%</h2></div><button className="icon-button" onClick={onClose} aria-label="Close">×</button></div>
    <p className="muted">{t(language,'readinessDetail')}</p>
    <div className="formula">round(100 × {numerator} ÷ {denominator}) = <b>{progress.readiness.readiness_pct}%</b></div>
    <div className="math-list">{progress.readiness.contributions.map(row=><div key={row.skill_id}><span>{row.name}</span><span>{Math.min(row.effective_level,row.target_level)} / {row.target_level}</span></div>)}</div>
  </section></div>
}

export function Loading({label='Loading…'}:{label?:string}) { return <div className="state-card"><span className="spinner"/><p>{label}</p></div> }
export function ErrorState({message,retry}:{message:string;retry?:()=>void}) { return <div className="state-card error"><h3>Something went wrong</h3><p>{message}</p>{retry&&<button className="secondary" onClick={retry}>Try again</button>}</div> }
export function Empty({children}:{children:React.ReactNode}) { return <div className="state-card"><p>{children}</p></div> }

export function Modal({title,onClose,children,wide=false}:{title:string;onClose:()=>void;children:React.ReactNode;wide?:boolean}) { return <div className="overlay centered" onMouseDown={onClose}><section className={`modal ${wide?'wide':''}`} role="dialog" aria-modal="true" aria-label={title} onMouseDown={e=>e.stopPropagation()}><div className="card-row"><h2>{title}</h2><button className="icon-button" onClick={onClose} aria-label="Close">×</button></div>{children}</section></div> }
