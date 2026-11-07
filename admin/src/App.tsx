import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'

type Turn = { role: 'user' | 'bot'; kind: 'chat' | 'voice'; text: string }
type Calc = Partial<Record<'leads' | 'late' | 'buy' | 'check' | 'risk', number>>
type Status = 'new' | 'noanswer' | 'contacted' | 'meeting' | 'done' | 'noshow' | 'lost'
type Lead = {
  id: number; ts: string; niche: string; name: string; via: 'phone' | 'telegram' | 'max'; contact: string
  source: 'form' | 'chat'; note: string; sid: string; page: string; calc: Calc; turns: Turn[]
  status: Status; meet_at: string | null; meet_link: string; reschedules: number; noshows: number; comment: string; tpl: string
}
type Dialog = { sid: string; start: string; niche: string; kinds: ('chat' | 'voice')[]; turns: Turn[]; has_lead: boolean }
type Todo = Record<'outcome' | 'new' | 'today' | 'tomorrow', number[]>
type Data = { spend: string; niches: Record<string, string>; leads: Lead[]; dialogs: Dialog[]; todo: Todo }
type Act = (id: number, action: string, extra?: object) => Promise<string>
type Talk = { title: string; sub: string; turns: Turn[] }

const VIA = { phone: 'телефон', telegram: 'Telegram', max: 'MAX' }
const KIND = { chat: 'чат', voice: 'голос' }
const CASES_URL = ''  // ссылка на кейсы для текста клиенту; пусто — в тексте «[ссылка на кейсы]»
const STATUS: Record<Status, [string, 'default' | 'secondary' | 'outline' | 'destructive']> = {
  new: ['новая', 'default'], noanswer: ['не дозвонился', 'destructive'], contacted: ['связался', 'secondary'], meeting: ['диагностика', 'default'],
  done: ['проведена', 'outline'], noshow: ['не пришёл', 'destructive'], lost: ['отказ', 'outline'],
}
// Тексты клиенту из marketing/sales/diagnostic-reglament.md. {когда} — «завтра в 14:00», {время} — «14:00», {где} — ссылка на встречу
const TPL: Record<string, [string, string]> = {
  qualify: ['Сразу после назначения', '{имя}, здравствуйте! Давайте ещё раз зафиксирую, на чём сошлись: {когда} МСК у нас диагностика {где}. Также отправляю наши кейсы — можете посмотреть их перед следующим звонком: {кейсы}'],
  day: ['За день', '{имя}, добрый день! Напоминаю: {когда} МСК у нас диагностика {где}. Всё в силе?'],
  h2: ['За 2 часа (если не ответит — позвонить)', '{имя}, через 2 часа наша встреча, ждём вас в {время} МСК. Если что-то изменилось — напишите прямо сейчас, успеем перенести.'],
  noshow1: ['Не пришёл — первый раз', '{имя}, вы были записаны на диагностику {когда}, но встреча не состоялась. У нас правило: если что-то меняется — предупреждаем минимум за 1 час, и того же ждём в ответ.\nПонимаю, что разные обстоятельства бывают, поэтому пойдём навстречу. Давайте назначим новую встречу. Большая просьба отметить её в календаре, чтобы точно присутствовать.\nПроговорю заранее: если без предупреждения не придёте повторно, диагностика станет платной. Вам удобнее завтра или послезавтра?'],
  noshow2: ['Не пришёл — второй раз', '{имя}, вы были записаны на диагностику {когда}, но встреча не состоялась.\nТак как вы не вышли на связь второй раз, следующая диагностика будет платной: 15 000 рублей.\nНапишите, если захотите записаться. Если сейчас неактуально — без обид, удачи с бизнесом.'],
  late1: ['Перенос в последний час — первый', 'Хорошо, записал вас на {когда} МСК. Проговорю ещё раз: более двух раз мы встречи не переносим, потому что бронируем конкретное время под вас. Сейчас у вас первый перенос.'],
  late2: ['Перенос в последний час — второй', 'Хорошо, записал вас на {когда} МСК. Напомню, что более двух раз встречи не переносим. Сейчас у вас второй перенос. Поэтому просим зафиксировать дату и время в календаре, чтобы встреча точно состоялась. Далее, к сожалению, не сможем провести диагностику бесплатно.'],
  early1: ['Перенос заранее — первый', 'Благодарю, что предупредили заранее. Просим подобрать время так, чтобы вы точно смогли присутствовать. Мы бронируем окно именно под вас и не можем записать другого человека. Подскажите удобные дату и время — посмотрю свободные слоты.'],
  early2: ['Перенос заранее — второй', 'Благодарю, что предупредили заранее. Мы придерживаемся правила: более двух раз встречи не переносим. Заранее договариваемся на удобную всем дату и просим отметить встречу в расписании. У нас свободно [время] и [время] — когда удобнее?'],
}

// время с сервера — МСК без зоны: «2026-10-22 14:05»
const when = (ts: string) =>
  new Date(ts.replace(' ', 'T') + '+03:00').toLocaleString('ru-RU', {
    day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Moscow',
  })
const msk = (s: string) => new Date(s.replace(' ', 'T') + ':00+03:00')
const nowMsk = () => new Date(Date.now() + 3 * 36e5).toISOString().slice(0, 16).replace('T', ' ')  // МСК всегда UTC+3
const addDays = (day: string, n: number) => new Date(Date.parse(day + 'T00:00:00Z') + n * 864e5).toISOString().slice(0, 10)
// «сегодня в 14:00», «завтра в 14:00», «24 октября в 14:00»
const meetWords = (at: string) => {
  const d = at.slice(0, 10), today = nowMsk().slice(0, 10)
  const day = d === today ? 'сегодня' : d === addDays(today, 1) ? 'завтра'
    : msk(at).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', timeZone: 'Europe/Moscow' })
  return `${day} в ${at.slice(11)}`
}
// «Петрова Анна Сергеевна» → «Анна», «Анна Петрова» → «Анна»
const firstName = (n: string) => {
  const w = n.trim().split(/\s+/)
  return w.length === 3 && /(вич|вна|чна|оглы|кызы)$/i.test(w[2]) ? w[1] : w[0]
}
const isNick = (c: string) => /[A-Za-z_]/.test(c)
const tgLink = (l: Lead) => isNick(l.contact)
  ? (l.via === 'telegram' ? `https://t.me/${l.contact.replace(/^@/, '')}` : '')  // ник в MAX — не Telegram
  : `https://t.me/+${l.contact.replace(/\D/g, '').replace(/^8(?=\d{10}$)/, '7')}`
const telLink = (l: Lead) => isNick(l.contact) ? '' : `tel:${l.contact.replace(/[^\d+]/g, '')}`
// Google Календарь владельца: открывается в его браузере, сервер ничего никуда не шлёт
const gcal = (l: Lead) => {
  const z = (d: Date) => d.toISOString().replace(/[-:]|\.\d{3}/g, '')
  const s = msk(l.meet_at!)
  const p = new URLSearchParams({ text: `Диагностика: ${l.name}`, details: [l.contact, l.meet_link].filter(Boolean).join('\n'), location: l.meet_link })
  return `https://calendar.google.com/calendar/render?action=TEMPLATE&dates=${z(s)}/${z(new Date(+s + 36e5))}&${p}`
}
const clientText = (l: Lead) => TPL[l.tpl]?.[1]
  .replace('{имя}', firstName(l.name))
  .replace('{когда}', l.meet_at ? meetWords(l.meet_at) : '[время]')
  .replace('{время}', l.meet_at?.slice(11) ?? '[время]')
  .replace('{где}', l.meet_link ? `по ссылке ${l.meet_link}` : 'в [Zoom/Телемост]')
  .replace('{кейсы}', CASES_URL || '[ссылка на кейсы]')
const rub = (v = 0) => Math.round(v).toLocaleString('ru-RU') + ' ₽'
const plural = (n: number, one: string, few: string, many: string) =>
  n % 10 === 1 && n % 100 !== 11 ? one : [2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100) ? few : many

export default function App() {
  const [data, setData] = useState<Data | null>(null)
  const [state, setState] = useState<'loading' | 'login' | 'ready' | 'error'>('loading')

  const load = async () => {
    const r = await fetch('/api/admin/data', { credentials: 'same-origin' }).catch(() => null)
    if (r?.status === 401) return setState('login')
    if (!r?.ok) return setState('error')
    setData(await r.json())
    setState('ready')
  }
  useEffect(() => { load() }, [])

  const logout = async () => {
    await fetch('/api/admin/logout', { method: 'POST', credentials: 'same-origin' }).catch(() => null)
    setData(null)
    setState('login')
  }

  if (state === 'loading') return <p className="p-6 text-muted-foreground">Загружаю…</p>
  if (state === 'error') return <p className="p-6">Не получилось загрузить. Обновите страницу.</p>
  if (state === 'login' || !data) return <Login onDone={load} />
  return <Main data={data} onLogout={logout} onReload={load} />
}

function Login({ onDone }: { onDone: () => void }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    const r = await fetch('/api/admin/login', {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password }),
    }).catch(() => null)
    setBusy(false)
    if (r?.ok) return onDone()
    setError(r?.status === 401 ? 'Неверный пароль' : r?.status === 429 ? 'Слишком много попыток. Подождите 10 минут.' : 'Нет связи с сервером. Попробуйте ещё раз.')
  }

  return (
    <div className="flex min-h-svh items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader><CardTitle className="text-xl">Заявки с сайта</CardTitle></CardHeader>
        <CardContent>
          <form onSubmit={submit} className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor="pw">Пароль</Label>
              <Input id="pw" type="password" autoComplete="current-password" autoFocus value={password}
                onChange={(e) => setPassword(e.target.value)} className="h-11 text-base" />
            </div>
            {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
            <Button type="submit" size="lg" className="h-11 text-base" disabled={busy || !password}>Войти</Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}

function Main({ data, onLogout, onReload }: { data: Data; onLogout: () => void; onReload: () => Promise<unknown> }) {
  const [talk, setTalk] = useState<Talk | null>(null)
  const [tab, setTab] = useState(location.hash.startsWith('#lead-') ? 'leads' : 'today')
  const [hl, setHl] = useState(0)
  const niche = (n: string) => data.niches[n] ?? n
  const byId = new Map(data.leads.map((l) => [l.id, l]))

  // ссылка из Telegram: /admin/#lead-12 — открываем заявки и прокручиваем к ней
  useEffect(() => {
    const go = () => {
      const id = Number(location.hash.match(/^#lead-(\d+)$/)?.[1])
      if (!id) return
      setTab('leads')
      setHl(id)
      setTimeout(() => document.getElementById(`lead-${id}`)?.scrollIntoView({ block: 'start' }), 100)
    }
    go()
    addEventListener('hashchange', go)
    return () => removeEventListener('hashchange', go)
  }, [])

  const act: Act = async (id, action, extra = {}) => {
    const r = await fetch('/api/admin/lead', {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id, action, ...extra }),
    }).catch(() => null)
    if (r?.status === 401) location.reload()
    if (!r?.ok) return (await r?.json().catch(() => null))?.error ?? 'Нет связи с сервером. Попробуйте ещё раз.'
    await onReload()
    return ''
  }

  const card = (l: Lead, anchor = false) => (
    <LeadCard key={l.id} lead={l} niche={niche(l.niche)} leads={data.leads} act={act} anchor={anchor} hl={anchor && hl === l.id}
      onTalk={() => setTalk({ title: `Переписка · заявка №${l.id}`, sub: `${l.name}, ${when(l.ts)}`, turns: l.turns })} />
  )
  const groups: [keyof Todo, string][] = [['outcome', 'Отметьте, как прошла диагностика'], ['new', 'Новые — позвоните или напишите'],
    ['today', 'Диагностики сегодня'], ['tomorrow', 'Диагностики завтра']]
  const todoCount = groups.reduce((n, [k]) => n + data.todo[k].length, 0)

  return (
    <div className="mx-auto max-w-2xl px-4 pb-10">
      <header className="flex items-start justify-between gap-3 pt-5 pb-4">
        <div>
          <h1 className="text-2xl font-semibold">Заявки</h1>
          <p className="mt-1 text-sm text-muted-foreground">Нейросеть сегодня: {data.spend}</p>
        </div>
        <Button variant="outline" onClick={onLogout}>Выйти</Button>
      </header>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList className="mb-3 w-full">
          <TabsTrigger value="today">Сегодня · {todoCount}</TabsTrigger>
          <TabsTrigger value="leads">Заявки · {data.leads.length}</TabsTrigger>
          <TabsTrigger value="dialogs">Диалоги · {data.dialogs.length}</TabsTrigger>
        </TabsList>

        <TabsContent value="today" className="flex flex-col gap-3">
          {todoCount === 0 && <p className="py-10 text-center text-muted-foreground">На сегодня всё сделано</p>}
          {groups.map(([k, title]) => data.todo[k].length > 0 && (
            <section key={k} className="flex flex-col gap-3">
              <h2 className="pt-2 text-base font-semibold">{title} · {data.todo[k].length}</h2>
              {data.todo[k].map((id) => byId.get(id)).filter((l) => !!l).map((l) => card(l))}
            </section>
          ))}
        </TabsContent>

        <TabsContent value="leads" className="flex flex-col gap-3">
          {data.leads.length === 0 && <p className="py-10 text-center text-muted-foreground">Заявок пока нет</p>}
          {data.leads.map((l) => card(l, true))}
        </TabsContent>

        <TabsContent value="dialogs" className="flex flex-col gap-3">
          <p className="text-sm text-muted-foreground">За 30 дней. Номера и почта скрыты.</p>
          {data.dialogs.length === 0 && <p className="py-10 text-center text-muted-foreground">Диалогов пока нет</p>}
          {data.dialogs.map((d) => (
            <button key={d.sid} type="button" className="text-left"
              onClick={() => setTalk({ title: 'Переписка', sub: `${niche(d.niche)}, ${when(d.start)}`, turns: d.turns })}>
              <Card className="py-4 transition-colors hover:bg-muted/50">
                <CardContent className="flex flex-col gap-2 px-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{when(d.start)}</span>
                    {d.has_lead && <Badge>заявка</Badge>}
                  </div>
                  <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
                    <Badge variant="secondary">{niche(d.niche)}</Badge>
                    <span>{d.kinds.map((k) => KIND[k]).join(' + ')}</span>
                    <span>· {d.turns.length} {plural(d.turns.length, 'ход', 'хода', 'ходов')}</span>
                  </div>
                </CardContent>
              </Card>
            </button>
          ))}
        </TabsContent>
      </Tabs>

      <Sheet open={!!talk} onOpenChange={(o) => !o && setTalk(null)}>
        <SheetContent className="gap-0 data-[side=right]:w-full data-[side=right]:sm:max-w-md">
          <SheetHeader className="border-b">
            <SheetTitle>{talk?.title}</SheetTitle>
            <SheetDescription>{talk?.sub}</SheetDescription>
          </SheetHeader>
          <div className="flex flex-col gap-3 overflow-y-auto p-4">
            {talk?.turns.map((t, i) => <Bubble key={i} turn={t} />)}
          </div>
        </SheetContent>
      </Sheet>
    </div>
  )
}

type CardProps = { lead: Lead; niche: string; leads: Lead[]; act: Act; anchor: boolean; hl: boolean; onTalk: () => void }

function LeadCard({ lead: l, niche, leads, act, anchor, hl, onTalk }: CardProps) {
  const [form, setForm] = useState<'' | 'set_meeting' | 'reschedule'>('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [comment, setComment] = useState(l.comment)
  const c = l.calc
  const page = l.page.replace(/^https?:\/\/[^/]+/, '')
  const past = !!l.meet_at && l.meet_at <= nowMsk()

  const run = async (action: string, extra?: object) => {
    setBusy(true)
    const err = await act(l.id, action, extra)
    setBusy(false)
    setError(err)
    if (!err) setForm('')
    return err
  }
  const btn = (label: string, action: string, extra?: object, variant: 'default' | 'outline' | 'destructive' = 'outline') =>
    <Button variant={variant} className="h-10" disabled={busy} onClick={() => run(action, extra)}>{label}</Button>

  return (
    <Card id={anchor ? `lead-${l.id}` : undefined} className={`scroll-mt-4 gap-3 py-4 ${hl ? 'ring-2 ring-primary' : ''}`}>
      <CardContent className="flex flex-col gap-2 px-4">
        <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
          <span>№{l.id}</span>
          <span>{when(l.ts)}</span>
          <Badge variant={STATUS[l.status][1]}>{STATUS[l.status][0]}</Badge>
          <Badge variant="secondary">{niche}</Badge>
          <Badge variant="outline">{l.source === 'chat' ? 'чат' : 'форма'}</Badge>
        </div>
        <div className="text-lg font-semibold">{l.name}</div>
        <Contact lead={l} />
        {l.meet_at && ['meeting', 'noshow', 'done'].includes(l.status) && (
          <p className="text-sm">
            Диагностика: <b>{meetWords(l.meet_at)}</b>
            {l.reschedules > 0 && <span className="text-muted-foreground"> · переносов {l.reschedules}</span>}
            {l.noshows > 0 && <span className="text-muted-foreground"> · не пришёл {l.noshows}</span>}
            {l.meet_link && <> · <a className="text-primary underline underline-offset-4" href={l.meet_link} target="_blank" rel="noreferrer">ссылка</a></>}
          </p>
        )}
        {c.risk != null && (
          <p className="text-sm">
            {c.leads != null && <>{c.leads} заявок в месяц, ждут {c.late}%, покупают {c.buy} из 100, чек {rub(c.check)}. </>}
            <b>Теряет {rub(c.risk)} в месяц</b>
          </p>
        )}
        {l.note && <p className="text-sm text-muted-foreground">{l.note}</p>}
        {page && page !== '/' && <p className="truncate text-xs text-muted-foreground">Страница: {page}</p>}

        <div className="mt-1 flex flex-wrap gap-2">
          {['new', 'noanswer'].includes(l.status) && btn('Связался', 'set_status', { status: 'contacted' }, 'default')}
          {['new', 'noanswer'].includes(l.status) && btn('Не дозвонился', 'set_status', { status: 'noanswer' })}
          {(['new', 'noanswer', 'contacted', 'noshow'].includes(l.status) || (l.status === 'meeting' && !l.meet_at)) && (
            <Button className="h-10" variant={['new', 'noanswer'].includes(l.status) ? 'outline' : 'default'} disabled={busy} onClick={() => setForm(form ? '' : 'set_meeting')}>
              Назначить диагностику
            </Button>
          )}
          {l.status === 'meeting' && l.meet_at && <>  {/* из Telegram «Назначена» приходит без времени — сначала время */}
            {btn('Проведена', 'done', {}, past ? 'default' : 'outline')}
            {btn('Не пришёл', 'noshow')}
            <Button variant="outline" className="h-10" disabled={busy} onClick={() => setForm(form ? '' : 'reschedule')}>Перенос</Button>
            <Button variant="outline" className="h-10" asChild><a href={gcal(l)} target="_blank" rel="noreferrer">В календарь</a></Button>
          </>}
          {['done', 'lost'].includes(l.status) ? btn('Вернуть в работу', 'set_status', { status: 'contacted' }) : btn('Отказ', 'lost')}
          {l.turns.length > 0 && <Button variant="outline" className="h-10" onClick={onTalk}>Переписка</Button>}
        </div>
        {form && <MeetForm lead={l} leads={leads} mode={form} busy={busy} onSubmit={run} onCancel={() => setForm('')} />}
        {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
        {l.tpl && TPL[l.tpl] && <ClientText lead={l} />}

        <textarea value={comment} onChange={(e) => setComment(e.target.value)} rows={2} placeholder="Комментарий для себя"
          className="mt-1 w-full rounded-lg border border-input bg-transparent px-2.5 py-2 text-base outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 md:text-sm" />
        {comment !== l.comment && (
          <Button className="h-10 self-start" disabled={busy} onClick={() => run('comment', { text: comment })}>Сохранить комментарий</Button>
        )}
      </CardContent>
    </Card>
  )
}

type FormProps = { lead: Lead; leads: Lead[]; mode: 'set_meeting' | 'reschedule'; busy: boolean; onSubmit: (action: string, extra: object) => Promise<string>; onCancel: () => void }

// Время по регламенту: с 10:00 до 18:00 МСК, не позже двух дней после заявки, между диагностиками — от часа
function MeetForm({ lead: l, leads, mode, busy, onSubmit, onCancel }: FormProps) {
  const [date, setDate] = useState(l.meet_at?.slice(0, 10) ?? addDays(nowMsk().slice(0, 10), 1))
  const [time, setTime] = useState(l.meet_at?.slice(11) ?? '11:00')
  const [link, setLink] = useState(l.meet_link)
  const at = `${date} ${time}`
  const early = mode === 'reschedule' && !!l.meet_at && +msk(l.meet_at) - Date.now() > 36e5
  const bad = !date || !/^\d\d:\d\d$/.test(time) ? 'Укажите дату и время'
    : time < '10:00' || time > '18:00' ? 'Диагностики — с 10:00 до 18:00 МСК'
    : at <= nowMsk() ? 'Это время уже прошло' : ''
  const warn = [
    date > addDays(l.ts.slice(0, 10), 2) && 'Больше двух дней после заявки — по правилам лучше пораньше.',
    ...leads.filter((o) => o.id !== l.id && o.status === 'meeting' && o.meet_at && !bad && Math.abs(+msk(o.meet_at) - +msk(at)) < 36e5)
      .map((o) => `Рядом другая диагностика: №${o.id} в ${o.meet_at!.slice(11)}.`),
  ].filter((w): w is string => !!w)

  return (
    <form className="mt-1 flex flex-col gap-3 rounded-lg border p-3"
      onSubmit={(e) => { e.preventDefault(); onSubmit(mode, mode === 'reschedule' ? { meet_at: at, early } : { meet_at: at, meet_link: link.trim() }) }}>
      <div className="font-medium">{mode === 'reschedule' ? 'Перенести на' : 'Диагностика'} (время МСК)</div>
      <div className="flex gap-2">
        <Field label="Дата"><Input type="date" value={date} min={nowMsk().slice(0, 10)} onChange={(e) => setDate(e.target.value)} className="h-10 text-base" /></Field>
        <Field label="Время"><Input type="time" value={time} min="10:00" max="18:00" step={900} onChange={(e) => setTime(e.target.value)} className="h-10 text-base" /></Field>
      </div>
      {mode === 'set_meeting' && (
        <Field label="Ссылка на Zoom или Телемост">
          <Input type="url" inputMode="url" placeholder="https://telemost.yandex.ru/…" value={link} onChange={(e) => setLink(e.target.value)} className="h-10 text-base" />
        </Field>
      )}
      {mode === 'reschedule' && (
        <p className="text-sm text-muted-foreground">{early ? 'Перенос заранее — больше чем за час.' : 'Перенос в последний час.'} Это {l.reschedules + 1}-й перенос.</p>
      )}
      {bad && <p className="text-sm text-destructive">{bad}</p>}
      {warn.map((w) => <p key={w} className="text-sm text-amber-700 dark:text-amber-400">{w}</p>)}
      <div className="flex gap-2">
        <Button type="submit" className="h-10" disabled={busy || !!bad}>Сохранить</Button>
        <Button type="button" variant="outline" className="h-10" onClick={onCancel}>Отмена</Button>
      </div>
    </form>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="flex flex-1 flex-col gap-1 text-sm text-muted-foreground">{label}{children}</label>
}

function ClientText({ lead: l }: { lead: Lead }) {
  const [copied, setCopied] = useState(false)
  const text = clientText(l)!
  const tg = tgLink(l), tel = telLink(l)
  const copy = async () => {
    await navigator.clipboard.writeText(text).catch(() => null)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }
  return (
    <div className="mt-1 flex flex-col gap-2 rounded-lg bg-muted p-3">
      <div className="text-sm font-medium">Текст клиенту · {TPL[l.tpl][0]}</div>
      <p className="text-[15px] leading-snug whitespace-pre-wrap select-all">{text}</p>
      <div className="flex flex-wrap gap-2">
        <Button className="h-10" onClick={copy}>{copied ? 'Скопировано' : 'Скопировать'}</Button>
        {tg && <Button variant="outline" className="h-10 bg-background" asChild><a href={tg} target="_blank" rel="noreferrer">Написать в Telegram</a></Button>}
        {tel && <Button variant="outline" className="h-10 bg-background" asChild><a href={tel}>Позвонить</a></Button>}
      </div>
    </div>
  )
}

function Contact({ lead: l }: { lead: Lead }) {
  const link = 'text-base font-medium text-primary underline underline-offset-4'
  if (/[A-Za-z_]/.test(l.contact)) {  // ник: в Telegram — ссылка, в MAX по нику ссылки нет
    return l.via === 'telegram'
      ? <a className={link} href={`https://t.me/${l.contact.replace(/^@/, '')}`}>{l.contact} · Telegram</a>
      : <span className="text-base">{l.contact} · {VIA[l.via]}</span>
  }
  const tel = l.contact.replace(/[^\d+]/g, '')
  return (
    <div className="text-base">
      <a className={link} href={`tel:${tel}`}>{l.contact}</a>
      {l.via !== 'phone' && <span className="text-muted-foreground"> · ответить в {VIA[l.via]}</span>}
    </div>
  )
}

function Bubble({ turn: t }: { turn: Turn }) {
  const me = t.role === 'user'
  return (
    <div className={me ? 'flex flex-col items-end' : 'flex flex-col items-start'}>
      <span className="mb-1 text-xs text-muted-foreground">{me ? 'Человек' : 'ИИ'}{t.kind === 'voice' ? ' · голос' : ''}</span>
      <div className={`max-w-[85%] rounded-2xl px-3 py-2 text-[15px] leading-snug whitespace-pre-wrap ${me ? 'bg-primary text-primary-foreground' : 'bg-muted'}`}>
        {t.text}
      </div>
    </div>
  )
}
