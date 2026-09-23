import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { aiApi, api, type AiAnswer, type AiStatus, type ChatTurn, type HrInsight, type Session } from './api'
import type { Language } from './i18n'

const PROMPTS: Record<Language, string[]> = {
  en: ['What should I focus on this month?', 'Why am I not ready for my target?', 'Give me something small this week', 'What happens if I complete the top recommendation?', 'Another way to improve communication'],
  ru: ['На чём сфокусироваться в этом месяце?', 'Почему я ещё не готов к цели?', 'Дайте что-нибудь небольшое на этой неделе', 'Что будет, если я пройду главную рекомендацию?', 'Другой способ развить коммуникацию'],
  kk: ['Осы айда неге назар аударуым керек?', 'Мақсатқа неге әлі дайын емеспін?', 'Осы аптаға жеңіл нәрсе беріңіз', 'Басты ұсынысты аяқтасам не болады?', 'Коммуникацияны дамытудың басқа жолы'],
}

const UI: Record<Language, Record<string, string>> = {
  en: { title:'Career AI', subtitle:'Answers only from your profile, gaps and the catalog', placeholder:'Ask about your development…', send:'Send', why:'Why this answer?', add:'Add to plan', open:'Open event', added:'Added to your plan', thinking:'Thinking…', llm:'AI', template:'Rule-based answer' },
  ru: { title:'Career AI', subtitle:'Отвечает только по вашему профилю, пробелам и каталогу', placeholder:'Спросите о своём развитии…', send:'Отправить', why:'Почему такой ответ?', add:'Добавить в план', open:'Открыть событие', added:'Добавлено в план', thinking:'Думаю…', llm:'AI', template:'Ответ по правилам' },
  kk: { title:'Career AI', subtitle:'Тек сіздің профиліңіз, олқылықтарыңыз және каталог бойынша жауап береді', placeholder:'Дамуыңыз туралы сұраңыз…', send:'Жіберу', why:'Неге осындай жауап?', add:'Жоспарға қосу', open:'Іс-шараны ашу', added:'Жоспарға қосылды', thinking:'Ойланып жатыр…', llm:'AI', template:'Ереже бойынша жауап' },
}

type Message = { role:'user'; text:string } | { role:'assistant'; answer:AiAnswer }

function ModeBadge({answer, language}:{answer:AiAnswer; language:Language}) {
  const ui = UI[language]
  return <span className={`ai-mode ${answer.mode}`} title={answer.fallback_reason || undefined}>
    {answer.mode === 'llm' ? `✦ ${ui.llm} · ${answer.model}` : ui.template} · {(answer.latency_ms / 1000).toFixed(1)}s
  </span>
}

export function CareerAIChat({session, language, languageMenu, onPlanChanged}:{session:Session; language:Language; languageMenu?:React.ReactNode; onPlanChanged:()=>void}) {
  const ui = UI[language]
  const navigate = useNavigate()
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [toast, setToast] = useState('')
  const endRef = useRef<HTMLDivElement>(null)
  useEffect(() => { endRef.current?.scrollIntoView?.({behavior:'smooth'}) }, [messages, busy])

  const ask = async (text:string) => {
    const question = text.trim()
    if (!question || busy) return
    const history:ChatTurn[] = messages.slice(-10).map(m => m.role === 'user' ? {role:'user', content:m.text} : {role:'assistant', content:m.answer.text})
    setMessages(current => [...current, {role:'user', text:question}])
    setInput(''); setBusy(true); setError('')
    try {
      const answer = await aiApi.chat(question, history, language, session.token)
      setMessages(current => [...current, {role:'assistant', answer}])
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }

  const addToPlan = async (eventId:string) => {
    if (!session.employeeId) return
    try { await api.activity(session.employeeId, eventId, 'in_progress', session.token); setToast(ui.added); onPlanChanged() }
    catch (e) { setToast((e as Error).message) }
    setTimeout(() => setToast(''), 2500)
  }

  return <>
    <header className="ai-header"><span>✦</span><div><h1>{ui.title}</h1><p>{ui.subtitle}</p></div>{languageMenu}</header>
    <main className="mobile-content ai-page">
      {messages.length === 0 && <div className="ai-bubble"><p>{language === 'ru' ? 'Выберите вопрос или задайте свой.' : language === 'kk' ? 'Сұрақ таңдаңыз немесе өзіңіз жазыңыз.' : 'Pick a question or ask your own.'}</p></div>}
      {messages.map((message, index) => message.role === 'user'
        ? <div className="user-bubble" key={index}>{message.text}</div>
        : <div className="ai-bubble" key={index}>
            <ModeBadge answer={message.answer} language={language}/>
            <p className="ai-answer-text">{message.answer.text}</p>
            {message.answer.actions.length > 0 && <div className="chip-row ai-actions">
              {message.answer.actions.map(action => action.type === 'add_to_plan'
                ? <button key={`a-${action.event_id}`} className="primary" onClick={() => void addToPlan(action.event_id)}>{ui.add}: {action.title}</button>
                : <button key={`o-${action.event_id}`} onClick={() => navigate('/events')}>{ui.open}</button>)}
            </div>}
            {message.answer.sources.length > 0 && <details><summary>{ui.why}</summary>
              {message.answer.sources.map((source, i) => <p key={i}><b>{source.tool}</b>: {source.summary}</p>)}
              {message.answer.fallback_reason && <p className="muted small">{message.answer.fallback_reason}</p>}
            </details>}
          </div>)}
      {busy && <div className="ai-bubble"><p className="muted">{ui.thinking}</p></div>}
      {error && <div className="ai-bubble"><p className="muted">{error}</p></div>}
      <div ref={endRef}/>
    </main>
    <div className="ai-composer">
      <div className="ai-prompts">{PROMPTS[language].map(prompt => <button key={prompt} onClick={() => void ask(prompt)} disabled={busy}>{prompt}</button>)}</div>
      <form onSubmit={e => { e.preventDefault(); void ask(input) }}>
        <input value={input} onChange={e => setInput(e.target.value)} placeholder={ui.placeholder} aria-label={ui.placeholder} maxLength={1000}/>
        <button className="primary" type="submit" disabled={busy || !input.trim()}>{ui.send}</button>
      </form>
    </div>
    {toast && <div className="toast">{toast}</div>}
  </>
}

export function HrAskAI({session}:{session:Session}) {
  const [question, setQuestion] = useState('')
  const [answer, setAnswer] = useState<AiAnswer>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const ask = async () => {
    if (!question.trim()) return
    setBusy(true); setError('')
    try { setAnswer(await aiApi.hrQuery(question, document.documentElement.lang === 'ru' ? 'ru' : 'en', session.token)) }
    catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  return <div className="hr-ask">
    <form className="ai-search" onSubmit={e => { e.preventDefault(); void ask() }}>
      ✦ <input value={question} onChange={e => setQuestion(e.target.value)} placeholder="Ask AI about skills, gaps, events…" aria-label="Ask AI"/>
      {busy && <small>…</small>}
    </form>
    {(answer || error) && <div className="hr-ask-panel">
      <button className="icon-button" aria-label="Close" onClick={() => { setAnswer(undefined); setError('') }}>×</button>
      {error ? <p>{error}</p> : answer && <>
        <ModeBadge answer={answer} language="en"/>
        <p className="ai-answer-text">{answer.text}</p>
        <p className="muted small">Based on de-identified aggregates only. Individual chats are never visible to HR.</p>
      </>}
    </div>}
  </div>
}

export function AiSettings({session}:{session:Session}) {
  const [status, setStatus] = useState<AiStatus>()
  const [key, setKey] = useState('')
  const [model, setModel] = useState('')
  const [message, setMessage] = useState('')
  const [insights, setInsights] = useState<{items:HrInsight[]; mode:string; model:string|null; reason:string|null}>()
  const [busy, setBusy] = useState(false)

  useEffect(() => { aiApi.status(session.token).then(s => { setStatus(s); setModel(s.model) }).catch(e => setMessage(e.message)) }, [session.token])

  const save = async () => {
    setBusy(true); setMessage('')
    try { const next = await aiApi.setKey(key.trim(), model.trim(), session.token); setStatus(next); setKey(''); setMessage('Key saved for this running server. It is kept in memory only and never shown again.') }
    catch (e) { setMessage((e as Error).message) } finally { setBusy(false) }
  }
  const clear = async () => { const next = await aiApi.clearKey(session.token); setStatus(next); setMessage('Runtime key removed.') }
  const generate = async () => {
    setBusy(true)
    try { const r = await aiApi.hrInsights('en', session.token); setInsights({items:r.insights, mode:r.mode, model:r.model, reason:r.fallback_reason}) }
    catch (e) { setMessage((e as Error).message) } finally { setBusy(false) }
  }

  return <main className="hr-content">
    <div className="hr-heading"><div><span className="eyebrow">AI LAYER</span><h1>AI settings & insights</h1></div>
      <span className="snapshot">{status ? (status.configured ? `Connected · ${status.model} · key ${status.key_hint ?? ''} (${status.source})` : 'No key · rule-based answers') : '…'}</span></div>
    <section className="panel ai-settings">
      <b>OpenAI API key</b>
      <p className="muted small">Paste a key to switch Career AI and HR insights from rule-based templates to the LLM. Recommendations stay deterministic; the model only explains data returned by the backend. Without a key everything still works.</p>
      <form onSubmit={e => { e.preventDefault(); void save() }}>
        <label>API key<input type="password" autoComplete="off" value={key} onChange={e => setKey(e.target.value)} placeholder="sk-..." aria-label="OpenAI API key"/></label>
        <label>Model<input value={model} onChange={e => setModel(e.target.value)} placeholder="gpt-4.1-mini" aria-label="Model"/></label>
        <div className="actions"><button className="primary" type="submit" disabled={busy || !key.trim()}>Save key</button>{status?.source === 'runtime' && <button type="button" className="secondary" onClick={() => void clear()}>Remove key</button>}</div>
      </form>
      {message && <p className="small">{message}</p>}
    </section>
    <section className="panel insight-panel ai-insights">
      <div className="card-row"><span className="ai-text">✦ AI insights</span><button className="secondary" onClick={() => void generate()} disabled={busy}>{busy ? 'Generating…' : 'Generate insights'}</button></div>
      {insights && <p className="muted small">{insights.mode === 'llm' ? `Generated by ${insights.model}` : `Rule-based (${insights.reason})`}</p>}
      {insights?.items.map((item, i) => <div key={i}>
        <b>{item.title}</b><p>{item.observation}</p>
        {item.evidence.length > 0 && <ul>{item.evidence.map((e, j) => <li key={j}>{e}</li>)}</ul>}
        <p><b>We don’t know:</b> {item.we_dont_know}</p><p><b>Suggested:</b> {item.suggested_action}</p>
      </div>)}
    </section>
  </main>
}
