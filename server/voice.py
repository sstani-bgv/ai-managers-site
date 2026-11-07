#!/usr/bin/env python3
"""Голосовой звонок с ИИ-менеджером Станислава (тот же персонаж, что в чате): браузер ⇄ /api/voice (этот релей) ⇄ Yandex AI Studio Realtime API.
Ключ остаётся на сервере. Из браузера пропускаем только звук и перебивание, обратно — только то, что нужно странице."""
import asyncio, json, os, re, time, urllib.request

import aiohttp
from aiohttp import web

from demo import GREETINGS, Spend, prompt

ENV = dict(l.split('=', 1) for l in open(os.path.join(os.environ['CREDENTIALS_DIRECTORY'], 'env')).read().split() if '=' in l)
ENV.update((k, v) for k, v in os.environ.items() if k.startswith(('YC_RT_', 'VOICE_')))  # не секреты можно задать и в Environment= юнита
MODEL = ENV.get('YC_RT_MODEL', 'speech-realtime-max-tts-live/latest')
URL = ENV.get('YC_RT_URL') or f"wss://ai.api.cloud.yandex.net/v1/realtime/openai?model=gpt://{ENV['YC_FOLDER']}/{MODEL}"
KEY = ENV.get('YC_RT_API_KEY') or ENV['YC_API_KEY']
ORIGIN = re.compile(r'https://bugaev-ai\.ru|http://(localhost|127\.0\.0\.1)(:\d+)?')
# Голос и стиль (role) на нишу. LiveTTS-голоса есть только у моделей *-tts-live, у старых — обычные голоса SpeechKit.
VOICES = ({'clinic': ('sofia', 'support'), 'school': ('vera', 'support'), 'opt': ('vasily', 'sales'), 'general': ('sergey', 'sales')}
          if 'tts-live' in MODEL else
          {'clinic': ('dasha', None), 'school': ('lera', None), 'opt': ('kirill', None), 'general': ('kirill', None)})
FEMALE = {'sofia', 'vera', 'irina', 'dasha', 'lera', 'alena', 'marina'}
RATE = 44100
CALL_SEC, IDLE_SEC = int(ENV.get('VOICE_CALL_SEC', 180)), int(ENV.get('VOICE_IDLE_SEC', 25))
MAX_LIVE, PER_IP_DAY = 5, 6
# ponytail: ₽ по секундам звонка — ≈6 ₽/мин из marketing/landing/running-costs.md, а не по usage из response.done.
# Считать по usage, если счёт Яндекса разойдётся с оценкой.
RUB_SEC, MIN_RUB = 0.1, 6  # меньше 6 ₽ (минуты) осталось — новых звонков нет
# доля голоса в 200 ₽ в день (см. ponytail у Spend в demo.py); без StateDirectory не стартуем — иначе лимит обнулялся бы при перезапуске
SPEND = Spend(os.path.join(os.environ['STATE_DIRECTORY'], 'spend-voice.json'), float(ENV.get('VOICE_RUB_DAY', 60)), 'voice')
TO_BROWSER = {'response.created', 'response.output_audio.delta', 'response.output_text.delta', 'response.output_audio_transcript.delta',
              'input_audio_buffer.speech_started', 'input_audio_buffer.speech_stopped',
              'conversation.item.input_audio_transcription.completed', 'response.done'}
TEXT = {'busy': 'Демо сейчас занято. Попробуйте позже или оставьте заявку ниже.',
        'limit': 'Время демо-звонка вышло. Продолжите текстом или оставьте заявку ниже.',
        'idle': 'Звонок завершён: вас не слышно. Нажмите кнопку, чтобы позвонить снова.',
        'error': 'Голосовое демо сейчас не работает. Напишите вопрос текстом или оставьте заявку ниже.'}

FATAL = {'upstream_unavailable', 'server_error', 'internal_error'}
DIALOG_URL = 'http://127.0.0.1:8081/internal/dialog'  # app.py; снаружи туда не попасть: Caddy пускает к нему только /api/*
SAY = ('response.output_audio_transcript.delta', 'response.output_text.delta')  # текст ответа ИИ кусками: обычно первый, второй — запасной


def fatal(err, heard):
    """Класть ли трубку на error от Yandex: сбой сервера — всегда, промах cancel/truncate — никогда, прочее — только до первого звука."""
    code, kind = str(err.get('code') or ''), str(err.get('type') or '')
    if code in FATAL or kind in FATAL:
        return True
    if re.search('cancel|truncat', f"{code} {err.get('message') or ''}", re.I):
        return False
    return not heard


# ponytail: число звонков в памяти одного процесса, сбрасывается при перезапуске — для демо достаточно; ₽ хранит SPEND в файле
live = {}  # ip -> сколько секунд выдано текущему звонку
day = {'date': '', 'calls': {}}


def admit(ip, now=None):
    """Сколько секунд можно говорить, 0 — отказ. 1 звонок на IP, ≤5 одновременно, ≤6 в день на IP, не дольше, чем хватит ₽ на сегодня."""
    date = time.strftime('%Y-%m-%d', time.localtime(now))
    if day['date'] != date:
        day.update(date=date, calls={})
    left = SPEND.left() - sum(live.values()) * RUB_SEC  # уже идущим звонкам их время зарезервировано
    if ip in live or len(live) >= MAX_LIVE or day['calls'].get(ip, 0) >= PER_IP_DAY or left < MIN_RUB:
        return 0
    day['calls'][ip] = day['calls'].get(ip, 0) + 1
    live[ip] = min(CALL_SEC, int(left / RUB_SEC))
    return live[ip]


def release(ip, secs):
    live.pop(ip, None)
    SPEND.add(secs * RUB_SEC)


async def spend(_):  # для страницы заявок (app.py) с 127.0.0.1: снаружи Caddy пускает сюда только /api/voice
    return web.json_response({'rub': round(SPEND.cap - SPEND.left(), 1), 'cap': SPEND.cap})


def from_browser(data):
    """Сообщение браузера → событие для Yandex. Всё остальное выбрасываем."""
    try:
        ev = json.loads(data)
        t = ev['type']
    except Exception:
        return None
    if t == 'input_audio_buffer.append' and isinstance(ev.get('audio'), str) and len(ev['audio']) <= 200_000:
        return {'type': t, 'audio': ev['audio']}
    if t == 'response.cancel':
        return {'type': t}
    if (t == 'conversation.item.truncate' and isinstance(ev.get('item_id'), str) and len(ev['item_id']) <= 100
            and isinstance(ev.get('audio_end_ms'), int) and 0 <= ev['audio_end_ms'] < 10**7):
        return {'type': t, 'item_id': ev['item_id'], 'content_index': 0, 'audio_end_ms': ev['audio_end_ms']}
    return None


def session(niche):
    voice, role = VOICES[niche]
    pcm = {'type': 'audio/pcm', 'rate': RATE}
    out = {'format': pcm, 'voice': voice, **({'role': role} if role else {})}
    she = '\nТы говоришь женским голосом: о себе — в женском роде («попросила», «уточню», «записала»).' if voice in FEMALE else ''
    return {'type': 'realtime', 'output_modalities': ['audio'],
            'instructions': f'{prompt(niche, voice=True)}\nПриветствие — твоя первая реплика: {GREETINGS[niche]}{she}',
            'audio': {'input': {'format': pcm, 'turn_detection': {'type': 'server_vad', 'threshold': 0.5, 'silence_duration_ms': 900}},
                      'output': out}}


def collect(ev, turns, buf):
    """Текст звонка (звук не храним): фраза человека — целиком, ответ ИИ — из кусков, по концу ответа. Конец звонка — collect({'type': 'response.done'}, …)."""
    t, said = ev.get('type'), str(ev.get('transcript') or '').strip()
    if t == 'conversation.item.input_audio_transcription.completed' and said:
        turns.append({'role': 'user', 'text': said})
    elif t in SAY:
        buf.setdefault(t, []).append(str(ev.get('delta') or ''))
    elif t in ('response.created', 'response.done'):
        text = ''.join(buf.get(SAY[0]) or buf.get(SAY[1]) or []).strip()
        buf.clear()
        if text:
            turns.append({'role': 'bot', 'text': text})


def save(sid, niche, turns):
    """Текст звонка → app.py: там маска номеров и почты и запись в базу на 30 дней. Зовётся в потоке, не держит звонки."""
    try:
        req = urllib.request.Request(DIALOG_URL, json.dumps({'sid': sid, 'niche': niche, 'turns': turns}).encode(), {'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=5).close()
    except Exception as e:
        print(f'voice dialog: {e!r}', flush=True)


async def relay(ws, up, allow, turns):
    """Гоняет события в обе стороны, пока кто-то не положит трубку. Возвращает причину конца. turns — сюда текст звонка."""
    buf = {}
    t0 = last = time.monotonic()
    heard = False  # был ли уже голос модели (см. fatal)

    async def down():
        nonlocal last, heard
        async for m in up:
            if m.type != aiohttp.WSMsgType.TEXT:
                continue
            ev = json.loads(m.data)
            t = ev.get('type')
            if t == 'error':
                err = ev.get('error') or {}
                print(f"voice upstream error {str(err.get('code') or err.get('type'))[:60]}", flush=True)
                if fatal(err, heard):
                    return 'error'
            elif t in TO_BROWSER:
                last, heard = time.monotonic(), heard or t == 'response.output_audio.delta'
                collect(ev, turns, buf)
                await ws.send_json({k: ev[k] for k in ('type', 'delta', 'transcript', 'item_id') if k in ev})
        return 'error'

    async def uplink():
        async for m in ws:
            if m.type == aiohttp.WSMsgType.TEXT and (ev := from_browser(m.data)):
                await up.send_json(ev)
        return 'hangup'

    async def clock():
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            if now - t0 >= allow:
                return 'limit'
            if now - last >= IDLE_SEC:
                return 'idle'

    tasks = [asyncio.create_task(c) for c in (down(), uplink(), clock())]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    collect({'type': 'response.done'}, turns, buf)  # ответ, который оборвал конец звонка
    return done.pop().result()


async def call(req):
    if not ORIGIN.fullmatch(req.headers.get('Origin', '')):
        return web.Response(status=403)
    niche = req.query.get('n') if req.query.get('n') in VOICES else 'general'
    sid, turns = req.query.get('sid', '')[:40], []  # sid проверит app.py
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=256 * 1024)
    await ws.prepare(req)
    ip = req.headers.get('X-Forwarded-For', req.remote or '').split(',')[0].strip()
    allow, reason = admit(ip), 'busy'
    if not allow:
        print(f'voice {niche} busy', flush=True)
    else:
        start, reason = time.monotonic(), 'error'
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s, \
                    s.ws_connect(URL, headers={'Authorization': 'Api-Key ' + KEY}, heartbeat=20) as up:
                await up.send_json({'type': 'session.update', 'session': session(niche)})
                await up.send_json({'type': 'response.create', 'response': {'instructions': f'Начни звонок этой фразой, почти дословно: {GREETINGS[niche]}'}})
                reason = await relay(ws, up, allow, turns)
        except Exception as e:
            print(f'voice: {type(e).__name__}', flush=True)
        finally:
            secs = time.monotonic() - start
            release(ip, secs)
            print(f'voice {niche} {secs:.0f}s {reason}', flush=True)
            if sid and turns:
                asyncio.get_running_loop().run_in_executor(None, save, sid, niche, turns)
    try:
        if reason in TEXT and not ws.closed:
            await ws.send_json({'type': 'end', 'text': TEXT[reason]})
        await ws.close()
    except Exception:
        pass
    return ws


if __name__ == '__main__':
    os.environ['TZ'] = 'Europe/Moscow'
    time.tzset()
    app = web.Application()
    app.router.add_get('/api/voice', call)
    app.router.add_get('/spend', spend)
    web.run_app(app, host='127.0.0.1', port=8082, access_log=None, print=None)
