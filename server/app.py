#!/usr/bin/env python3
"""Заявки с bugaev-ai.ru: пишем в SQLite на сервере в РФ (152-ФЗ), в Telegram шлём только номер заявки и ссылку."""
import contextlib, hashlib, hmac, json, os, re, sqlite3, subprocess, sys, threading, time, urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from demo import GREETINGS, Demo, biz_chips


def pw_hash(pw, salt=None, iters=600_000):
    """Строка для ADMIN_PASS: pbkdf2$<итераций>$<соль hex>$<хеш hex>."""
    salt = salt or os.urandom(16)
    return f'pbkdf2${iters}${salt.hex()}${hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iters).hex()}'


def pw_ok(pw, stored):
    try:
        _, iters, salt, _h = stored.split('$')
        return hmac.compare_digest(pw_hash(pw, bytes.fromhex(salt), int(iters)), stored)
    except ValueError:
        return False


if __name__ == '__main__' and sys.argv[1:] == ['hash-password']:  # echo -n 'пароль' | python3 app.py hash-password → строка для ADMIN_PASS
    print(pw_hash(sys.stdin.read().rstrip('\n')))
    sys.exit()

ENV = dict(l.split('=', 1) for l in open(os.path.join(os.environ['CREDENTIALS_DIRECTORY'], 'env')).read().split() if '=' in l)
ENV.update((k, v) for k, v in os.environ.items() if k.startswith(('YC_CHAT_', 'CHAT_')))  # модель и лимит ₽ чата можно сменить в Environment= юнита, как у голоса
STATE = os.environ.get('STATE_DIRECTORY', '/var/lib/bugaev-leads')
DB = os.path.join(STATE, 'leads.db')
SITE = 'https://bugaev-ai.ru'
NICHES = {'general': 'общий', 'school': 'онлайн-школа', 'opt': 'опт', 'clinic': 'клиника'}
CALC_KEYS = ('leads', 'late', 'buy', 'check', 'risk')
VIA = {'phone': 'телефон', 'telegram': 'Telegram', 'max': 'MAX'}
DEMO = Demo(ENV, STATE)
BUSY = json.dumps({'error': 'ИИ-менеджер сейчас занят. Оставьте заявку в форме ниже — Станислав ответит сам.'}, ensure_ascii=False)


@contextlib.contextmanager
def db():
    # `with sqlite3.connect()` только коммитит, но не закрывает: соединения копились, за 3 дня кончились 1024 файла и сервер завис
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()


def init_db():
    with db() as c:
        c.execute('create table if not exists leads(id integer primary key, ts text, name text, via text, contact text, niche text,'
                  ' calc text, page text, ref text, ip text, ua text, notified int default 0)')
        cols = {r['name'] for r in c.execute('pragma table_info(leads)')}
        # откуда заявка: форма или чат; sid — чья переписка. Воронка: status, время встречи (МСК), переносы, неявки, комментарий;
        # rem — ключи отправленных напоминаний (сброс при новом времени), last/acted — последнее действие и когда (какой текст клиенту показать)
        for col, sql in (('source', "text default 'form'"), ('note', "text default ''"), ('sid', "text default ''"), ('status', "text default 'new'"),
                         ('meet_at', 'text'), ('meet_link', "text default ''"), ('reschedules', 'int default 0'), ('noshows', 'int default 0'),
                         ('comment', "text default ''"), ('rem', "text default '[]'"), ('last', "text default ''"), ('acted', "text default ''")):
            if col not in cols:
                c.execute(f'alter table leads add column {col} {sql}')
        # Переписка с ИИ (чат и текст звонков) на 30 дней, чтобы ИИ отвечал лучше. Звук не храним, контакты — «***» (они в заявке).
        c.execute('create table if not exists dialogs(id integer primary key, ts text, sid text, niche text, kind text, role text, text text)')
        c.execute('create table if not exists digests(day text primary key)')  # дни, за которые утренняя сводка уже ушла


init_db()

hits = {}  # ponytail: память на процесс, хватит при десятках заявок в день; при спаме — fail2ban на уровне Caddy


def limited(ip, kind='lead', n=5):
    now, k = time.time(), (kind, ip)
    hits[k] = [t for t in hits.get(k, []) if now - t < 600] + [now]
    return len(hits[k]) > n


def clean(v, n):
    return str(v or '').strip()[:n]


SID = re.compile(r'[0-9A-Fa-f-]{8,40}')  # crypto.randomUUID() из demo.js


def sid_of(d):
    s = d.get('sid')
    return s if isinstance(s, str) and SID.fullmatch(s) else ''


EMAIL = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
NICK = re.compile(r'(?<![\w.])@\w+')
PHONE = re.compile(r'\+?[\d(][\d ()\u00a0-]*\d')  # цифры со скобками, пробелами, дефисами
THOUSANDS = re.compile(r'\d{1,3}(?:[ \u00a0]\d{3})+')  # «12 000 000 ₽» — сумма, не номер


def mask(t):
    """Почта, @ник и номер (от 7 цифр) → «***». «80 000 ₽», «300 заявок», «12 000 000» остаются."""
    t = NICK.sub('***', EMAIL.sub('***', t))
    return PHONE.sub(lambda m: '***' if len(re.sub(r'\D', '', m[0])) >= 7 and not THOUSANDS.fullmatch(m[0]) else m[0], t)


def since(days=30):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() - days * 86400))


def save_turns(sid, niche, kind, turns):
    """Ходы разговора [(role 'user'|'bot', текст)] в базу, с маской контактов."""
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    with db() as c:
        c.executemany('insert into dialogs(ts, sid, niche, kind, role, text) values (?,?,?,?,?,?)',
                      [(ts, sid, niche, kind, role, mask(text)) for role, text in turns])


def purge():
    """Переписка старше 30 дней удаляется (так написано в politika.html)."""
    with db() as c:
        c.execute('delete from dialogs where ts < ?', (since(),))


def dialogs():
    """{sid: [ходы по порядку]} за 30 дней. ponytail: все строки разом — хватит на тысячи диалогов в месяц; больше — запрос с limit по сессиям."""
    out = {}
    with db() as c:
        for r in c.execute('select * from dialogs where ts >= ? order by id', (since(),)):
            out.setdefault(r['sid'], []).append(r)
    return out


def rub(v):
    return f'{int(v):,}'.replace(',', ' ') + ' ₽'


def lead_fields(d):
    """Имя и контакт по правилам /api/lead. None — не годится."""
    name, contact = clean(d.get('name'), 80), clean(d.get('contact'), 40)
    if d.get('via') in VIA and len(contact) >= 5 and name:
        return {'name': name, 'via': d['via'], 'contact': contact}


def contact_ok(via, v):
    """Проверки контакта как у формы на странице (VIA[...].ok в index.html), но в чате MAX, как Telegram, принимает и ник."""
    digits = len(re.sub(r'[^0-9]', '', v))
    if via in ('telegram', 'max'):
        return bool(re.fullmatch(r'@?[A-Za-z0-9_]{5,32}', v)) or digits >= 10
    return bool(re.fullmatch(r'\+?[0-9\s()-]{10,20}', v)) and digits >= 10


LEAD_TAG = re.compile(r'\[\[\s*LEAD\b(.*?)(?:\]\]|\Z)', re.S)  # обрезанная метка (кончились токены) тоже вырезается
CHIPS_TAG = re.compile(r'\[\[\s*CHIPS\b(.*?)(?:\]\]+|\Z)', re.S)  # [[CHIPS ["…","…"]]]: закрывающие скобки списка и метки съедаются вместе
FAKE_TURN = re.compile(r'^\s*(Пользователь|Ассистент|Клиент|Менеджер|User|Assistant)\s*:', re.M | re.I)
# Не хватает данных для заявки — спрашиваем только недостающее. «Telegram или MAX» в ASK_AGAIN/ASK_HELP нет: иначе это сочтётся новым вопросом «куда удобнее».
ASK_AGAIN = 'Чтобы передать заявку Станиславу, напишите, пожалуйста: как вас зовут и ваш номер телефона или ник.'
ASK_AGAIN_2 = ASK_AGAIN.replace('напишите,', 'напишите ещё раз,')  # «ещё раз» — только если ИИ уже спрашивал имя или контакт
NAME_Q = 'Как вас зовут?'  # контакт есть, имени нет
ASK_CONTACT = 'Куда удобнее ответить: телефон, Telegram или MAX? Напишите номер телефона или ник.'  # имя есть, канала нет
ASK_BY_VIA = {'phone': 'Напишите, пожалуйста, номер телефона цифрами.',  # имя и канал есть — нужен только контакт
              'telegram': 'Напишите, пожалуйста, ваш ник в Telegram или номер телефона.',
              'max': 'Напишите, пожалуйста, номер телефона или ник в MAX.'}
ASK_HELP = 'Не получилось распознать контакт. Напишите номер телефона цифрами или ник в Telegram — или оставьте заявку в форме ниже.'  # вместо повтора
CARD = 'Проверьте карточку ниже, отметьте согласие и нажмите «Отправить Станиславу».'
PROMISE = re.compile(r'свяжется|позвонит|напишет вам|перезвонит|заявка отправлена|передал', re.I)  # до кнопки так говорить нельзя: заявка ещё не ушла
REDIRECT = 'Я помогаю с вопросами об ИИ-менеджере для бизнеса. Что хотите узнать?'  # вместо отказа слово в слово, как в прошлом ответе
THANKS = re.compile(r'(?:\W*(?:спасибо|большое|понятно|ясно|я|пока|подумаю|подумаем|мы)\b)+\W*', re.I)  # всё сообщение — «спасибо», «понятно», «подумаю»
TAIL_Q = re.compile(r'[^.!?\n]*\?\s*$')  # вопрос в конце ответа
# Приглашение на разбор — вопрос с «записать/записаться/запишу», «хотите… разбор» или «Станислав… разберёт». «Отлично, запишу вас на разбор.» — не вопрос, не трогаем.
INVITE = re.compile(r'[^.!?\n]*(?:\bзапи(?:сать|саться|шу)\b|(?:хотите|станислав)[^.!?\n]*разб[оеё]р)[^.!?\n]*\?', re.I)
JUST_LOOKING = re.compile(r'просто (?:хочу )?(?:понять|узнать)|только узнать|подума', re.I)
# Вопросы о бизнесе по порядку из промпта: каждый задаём один раз; вместо выкинутого приглашения или повтора — первый ещё не заданный
NEXT_Q = (('заявок в месяц', 'Сколько заявок в месяц у вас приходит?', ['до 100', '100–500', 'больше 500']),
          ('откуда', 'Откуда к вам приходят заявки?', ['сайт', 'мессенджеры', 'звонки', 'реклама']),
          ('занимает', 'Чем занимается ваш бизнес?', None))  # None — варианты по нише страницы
QUESTION = re.compile(r'[^.!?\n]*\?')  # «Запуск занимает 1–2 недели.» — не вопрос про бизнес
# Человек сам назвал, где пишут клиенты (не в своём вопросе: «В каких мессенджерах работает?»), — «Откуда приходят заявки?» уже не спрашиваем. «почти» — не почта.
CHANNELS = re.compile(r'сайт|whatsapp|ватсап|вотсап|telegram|телеграм|вконтакт|\bвк\b|\bmax\b|в\s+макс|\bпочт[аеуо]|\bзвон|мессенджер', re.I)
LEADISH = re.compile(r'карточк|^\W*спасибо', re.I)  # модель делает вид, что приняла заявку
# Время звонка в заметке: «после 19», «19:00», «вечером», «в субботу»…
TIME = re.compile(r'\b\d{1,2}[:.]\d{2}\b|\b(?:после|до|с|в|к|около)\s+\d{1,2}\b|\b(?:утр|вечер|днём|днем|ноч|завтра|сегодня|выходн|будн'
                  r'|понедельн|вторник|среду|четверг|пятниц|суббот|воскресен)', re.I)
MAX = re.compile(r'\bmax\b|\bв\s+макс[еу]?\b', re.I)
TG = re.compile(r'telegram|телеграм|\bтг\b', re.I)
CHANNEL = re.compile(r'телефон|звон|telegram|телеграм|\bтг\b', re.I)
ASK_NAME = re.compile(r'зовут|\bимя\b', re.I)  # ИИ спросил имя: следующий ответ человека — имя
NOT_NAME = {'да', 'нет', 'ок', 'хорошо', 'ладно', 'давайте', 'спасибо', 'телефон', 'telegram', 'телеграм', 'зачем', 'здравствуйте', 'здравствуй',
            'привет', 'добрый', 'доброе', 'алло', 'пожалуйста', 'вот', 'конечно', 'отлично', 'звоните', 'позвоните', 'перезвоните', 'пишите',
            'напишите', 'номер', 'мой'}
CONTACT = re.compile(r'(?<![\w.])@[A-Za-z0-9_]{5,32}\b|\+?[\d(][\d\s()-]{8,}\d')  # @ник (не почта) или номер
NAME = r'[А-ЯЁ][а-яё]+(?:[ -][А-ЯЁ][а-яё]+)?'  # «Ольга», «Анна Петрова», «Анна-Мария»
NAME_FIRST = re.compile(rf'({NAME})\s*[,—–-]\s*({CONTACT.pattern})')  # сообщение начинается с «Ольга, +7 912…» или «Анна — @anna_x»
# «меня зовут Ольга» где угодно; «я Ольга», «это Ольга» — в начале сообщения или после знака: «Здравствуйте, это Ольга»
SELF_NAME = re.compile(rf'(?i:меня\s+зовут)\s+([^\W\d_]+(?:[ -][А-ЯЁ][а-яё]+)?)|(?:^|[,.!?]\s*)(?i:я|это)\s+({NAME})(?![\w-])')
ASKED_VIA = re.compile(r'куда удобнее|telegram или max', re.I)  # ИИ спросил, куда ответить: канал ищем только в ответах после этого
# Вопрос про имя, номер или ник: варианты-кнопки тут — выдуманные личные данные. «Куда удобнее ответить — телефон, Telegram или MAX?» сюда не попадает.
ASK_PERSONAL = re.compile(r'зовут|\bимя\b|номер|\bник\b', re.I)
REAL_NAME = re.compile(r'(?:настоящ|реальн)\w*\s+им[яе]', re.I)  # модель спорит с именем: «напишите ваше настоящее имя»
SENT = re.compile(r'(?<=[.!?])\s+|\n')  # границы предложений
EMPTY = re.compile(r'карточк|^\W*спасибо\b|^\W*(?:ок|окей|хорошо|отлично|понятно|понял[аи]?|принято)?\W*$', re.I)  # без заявки — пустые фразы


def words(s):
    return ' ' + ' '.join(re.findall(r'\w+', s.lower().replace('ё', 'е'))) + ' '


def said_name(name, hist):
    """Имя целым словом есть в сообщениях человека."""
    w = words(name)
    return not w.isspace() and w in words('\n'.join(m['text'] for m in hist if m['role'] == 'user'))


def said_by_user(lead, hist):
    """Имя и контакт есть в сообщениях самого человека. Модель любит дописать реплики за него и выдумать «Иван, @Ivan_T»."""
    mine = [m['text'] for m in hist if m['role'] == 'user']
    if not said_name(lead['name'], hist):
        return False
    c = lead['contact']
    if re.search(r'[A-Za-z_]', c):  # ник в Telegram или MAX
        return bool(re.search(rf'(?<!\w){re.escape(c.lstrip("@"))}(?!\w)', '\n'.join(mine), re.I))
    tail = re.sub(r'\D', '', c)[-7:]  # номер могли записать по-разному: сверяем последние 7 цифр
    return any(tail in re.sub(r'\D', '', t) for t in mine)


def time_ok(part, said):
    """Время в заметке оставляем, только если человек сам его назвал."""
    for m in TIME.finditer(part):
        n = re.search(r'\d+', m.group())
        if not re.search(rf'(?<!\d){n.group()}(?!\d)' if n else re.escape(m.group()), said, re.I):
            return False
    return True


def chosen_via(hist):
    """(канал, который назвал человек: 'max', 'telegram', 'phone' или '', спрашивал ли ИИ «куда удобнее…»).
    Канал ищем в ответах после последнего такого вопроса: «клиенты пишут нам в MAX» раньше — не выбор."""
    # ponytail: если ИИ так и не спросил, смотрим все сообщения — тогда «клиенты пишут в MAX» ещё сработает
    k = max((i for i, m in enumerate(hist) if m['role'] == 'assistant' and ASKED_VIA.search(m['text'])), default=-1)
    last = next((m['text'] for m in reversed(hist[k + 1:]) if m['role'] == 'user' and (MAX.search(m['text']) or CHANNEL.search(m['text']))), '')
    return ('max' if MAX.search(last) else 'telegram' if TG.search(last) else 'phone' if last else ''), k >= 0


def known_name(d, hist):
    """Имя, которое человек написал сам: из метки модели, из ответа на «Как вас зовут?», из «Ольга, +7 912…» или «меня зовут Ольга». '' — не знаем."""
    name = clean(d.get('name'), 80) if isinstance(d, dict) else ''
    if said_name(name, hist):
        return name
    for q, a in reversed(list(zip(hist, hist[1:]))):
        if q['role'] == 'assistant' and a['role'] == 'user' and ASK_NAME.search(q['text']):
            t = re.sub(r'^(?:меня зовут|зовут|я)\s+', '', CONTACT.sub(' ', a['text']).strip(' ,.!'), flags=re.I).strip(' ,.!')
            # ответ на вопрос об имени — имя как написано: «Мария», «Мария, @tatyana» → «Мария», «ТЕСТ QA чат (удалить)», «ООО Ромашка», «Kate»;
            # «да», «спасибо», вопрос в ответ — не имя. ponytail: короткий список не-имён; если в карточки полезут другие слова — дополнить его
            if 0 < len(t) <= 40 and '?' not in t and not set(words(t).split()) & NOT_NAME:
                return t
    for m in reversed([m['text'] for m in hist if m['role'] == 'user']):  # имя без вопроса: «Ольга, +7 912…», «Меня зовут Ольга», «Здравствуйте, это Ольга»
        f = NAME_FIRST.match(m)
        s = f if f and contact_in(f[2]) else SELF_NAME.search(m)
        # ponytail: «Это Битрикс.» тоже сойдёт за имя; карточка будет, только если следом придёт контакт, и имя человек увидит в ней до отправки
        t = s and (s[1] or s[2])
        if t and not set(words(t).split()) & NOT_NAME:
            return t
    return ''


def contact_in(t):
    """Контакт в сообщении человека: @ник или номер от 10 цифр. None — нет."""
    for m in CONTACT.finditer(t):
        c = m.group().strip()
        if contact_ok('telegram' if c.startswith('@') else 'phone', c):
            return c


def asks_contact(t):
    """Вопрос или просьба об имени, номере, нике или канале: «Как вас зовут?», «Напишите номер…». «500 ₽ в месяц за номер.» — не просьба."""
    return any(re.search(r'\?$|\b(?:напишите|укажите|оставьте|пришлите|назовите|сообщите)\b', p, re.I) and (ASK_PERSONAL.search(p) or ASKED_VIA.search(p))
               for p in SENT.split(t.strip()))


def useful(text):
    """Ответ модели без пустых фраз, обещаний и своих просьб об имени и контакте: их заменит наш вопрос о недостающем. '' — ничего полезного."""
    t = '\n'.join(filter(None, (' '.join(p for p in SENT.split(line) if not (EMPTY.search(p) or PROMISE.search(p) or asks_contact(p)))
                                for line in text.split('\n'))))
    return '' if t.endswith(':') else t


def ask_missing(name, via, hist):
    """Спрашиваем только недостающее; тот же вопрос о контакте дважды подряд не шлём."""
    if name:
        q = ASK_BY_VIA.get(via, ASK_CONTACT)
    elif any(contact_in(m['text']) for m in hist if m['role'] == 'user'):
        q = NAME_Q
    else:
        q = ASK_AGAIN_2 if any(asks_contact(m['text']) for m in hist if m['role'] == 'assistant') else ASK_AGAIN
    prev = next((m['text'].strip() for m in reversed(hist) if m['role'] == 'assistant'), '')
    return ASK_HELP if prev.endswith(q) and q != NAME_Q else q


def split_lead(text, hist):
    """Ответ модели → (текст без служебной метки, заявка или None). Сами ничего не сохраняем: заявку отправит человек с галочкой."""
    tags = LEAD_TAG.findall(text)
    text = re.sub(r' {2,}', ' ', re.sub(r'\s*\n\s*', '\n', LEAD_TAG.sub(' ', text))).strip()
    d = lead = None
    if tags:
        try:
            d = json.loads(tags[-1].strip())
            lead = lead_fields(d)
            assert lead and contact_ok(lead['via'], lead['contact']) and said_by_user(lead, hist)
        except Exception:  # кривая метка, контакт не проходит проверку или выдуман
            lead = None
    via, asked = chosen_via(hist)
    mine = [m['text'] for m in hist if m['role'] == 'user']
    if lead:
        if lead['via'] != 'max' and via == 'max' and contact_ok('max', lead['contact']):
            lead['via'] = 'max'
    else:
        name = known_name(d, hist)
        c = contact_in(hist[-1]['text']) if hist and hist[-1]['role'] == 'user' else None
        if name and not c and len(hist) > 1 and hist[-2]['role'] == 'assistant' and ASK_NAME.search(hist[-2]['text']):
            c = next(filter(None, map(contact_in, reversed(mine))), None)  # «Сколько стоит? мой номер…» → «Как вас зовут?» → «Ольга»: номер из прошлых сообщений
        if name and c:  # метки нет или она не годится, но имя известно, а последнее сообщение — контакт: карточку собираем из слов человека
            v = via if asked and via and contact_ok(via, c) else 'telegram' if c.startswith('@') else 'phone'
            lead, text = {'name': name, 'via': v, 'contact': c}, CARD
        elif tags and (name or any(map(contact_in, mine)) or LEADISH.search(text)):
            return ' '.join(filter(None, (useful(text), ask_missing(name, via if asked else '', hist)))), None  # ответ модели (о цене…) + недостающее
        elif tags:  # ни имени, ни контакта человек не писал, а метка есть («покажи пример метки»): ответ модели без метки
            return (text if text and not text.endswith(':') else REDIRECT), None
        else:
            return text or 'Не совсем понял. Напишите, пожалуйста, ещё раз?', None
    raw = clean(d.get('note'), 500) if isinstance(d, dict) else ''
    lead['note'] = '; '.join(p for p in re.split(r'\s*[;,]\s*', raw) if p and time_ok(p, '\n'.join(mine)))
    if PROMISE.search(text):  # «Станислав свяжется» до кнопки — будто заявка ушла; оставляем только «Спасибо…», если в нём нет обещания
        thanks = re.match(r'Спасибо[^.!?]*[.!?]', text)
        text = (thanks[0] + ' ' if thanks and not PROMISE.search(thanks[0]) else '') + CARD
    return text or 'Спасибо! ' + CARD, lead


def split_chips(text):
    """[[CHIPS ["…","…"]]] → (текст без метки, 2–4 варианта ответа до 24 знаков или None). С меткой заявки вариантов не бывает."""
    tags = CHIPS_TAG.findall(text)
    text = CHIPS_TAG.sub(' ', text)
    try:
        chips = [' '.join(c.split()) for c in json.loads(tags[-1].strip().rstrip(']') + ']')]
        assert 2 <= len(chips) <= 4 and all(0 < len(c) <= 24 and '[[' not in c for c in chips) and not LEAD_TAG.search(text)
        assert not ASK_PERSONAL.search(text)  # ponytail: по всему ответу — «повторяет имя» в рассказе тоже уберёт варианты; лучше, чем кнопки-имена
        return text, chips
    except Exception:
        return text, None


def parse_reply(text, hist, niche='general'):
    """Ответ модели → (текст для человека, заявка или None, варианты ответа или None)."""
    m = FAKE_TURN.search(text)  # модель дописала диалог за человека: с этой строки всё выдумано
    text, chips = split_chips(text[:m.start()] if m else text)
    text, lead = split_lead(text, hist)
    if lead:
        return text, lead, None  # карточку сервер мог собрать сам — тогда вариантов нет
    if REAL_NAME.search(text):  # имя не оспариваем: спрашиваем следующий шаг — куда ответить (живой случай «ТЕСТ QA чат (удалить)»)
        via, asked = chosen_via(hist)
        return ask_missing(known_name(None, hist), via if asked else '', hist), None, None
    last = hist[-1]['text'].strip().lower() if hist else ''
    mine, said = ([m['text'] for m in hist if m['role'] == r] for r in ('user', 'assistant'))
    prev = said[-1] if said else ''
    text = text.replace('Хотите записать вас', 'Записать вас')  # «Хотите записать вас…?» — не по-русски
    # Вопросы о бизнесе, которые ИИ уже задавал (каналы человек назвал сам — «откуда» тоже), и следующий по порядку; на странице ниши бизнес известен
    asked = ' '.join(q.lower() for t in said for q in QUESTION.findall(t)) + (' откуда' if any(CHANNELS.search(QUESTION.sub('', t)) for t in mine) else '')
    ask_next = next(((q, c or biz_chips(niche)) for k, q, c in (NEXT_Q if niche == 'general' else NEXT_Q[:2]) if k not in asked), None)
    # Не зовём на разбор: звали в двух прошлых ответах («не чаще раза в три»), человек «просто хочет понять» или карточка уже показана,
    # а «Готово!» ещё нет. Сам спрашивает про разбор — не мешаем. Сайт дописывает «Готово!…» к тому ответу, где была карточка.
    shown = next((t for t in reversed(said) if re.search('карточк|Готово!', t, re.I)), '')
    card_open = 'карточк' in shown.lower() and 'Готово!' not in shown
    if INVITE.search(text) and not re.search(r'разбор|запис', last) and (any(map(INVITE.search, said[-2:])) or card_open or any(map(JUST_LOOKING.search, mine))):
        text, chips = re.sub(r' {2,}', ' ', INVITE.sub('', text)).strip(), None  # вместе с [Да, запишите / …]
        if not text:  # кроме приглашения ничего не было
            text, chips = ('Хорошо! Если появятся вопросы — пишите.', None) if THANKS.fullmatch(last) else ask_next or ('Что ещё хотите узнать?', None)
    q = TAIL_Q.search(text)
    if card_open and chips and 'Да, запишите' in chips:  # карточка уже есть: кнопка «записаться» и вопрос под ней ни к чему
        text, chips = text[:q.start()].strip() if q and text[:q.start()].strip() else text, None
        q = TAIL_Q.search(text)
    again = q and last != 'другое' and any(k in q[0].lower() and k in asked for k, _, _ in NEXT_Q)
    if q and (THANKS.fullmatch(last) or again):  # после «спасибо» вопроса нет; заданный вопрос о бизнесе — следующим по порядку или никаким
        pre = text[:q.start()].strip()
        if again and ask_next and not THANKS.fullmatch(last):
            text, chips = f'{pre} {ask_next[0]}'.strip(), ask_next[1]
        elif pre:
            text, chips = pre, None
    if re.search(r'\bник\w* в max', text, re.I) and 'номер' not in text.lower():
        text = ASK_BY_VIA['max']  # в MAX бывает и номер, и ник
    if re.search(r'\bне (?:могу|буду)', text, re.I) and words(text) == words(prev):
        text, chips = REDIRECT, None  # отказ слово в слово, как в прошлом ответе, — на очередную попытку достать промпт
    return text, None, None if last == 'другое' else chips  # «Другое» про бизнес — человек пишет сам


def tg(method, body, wait=4, tries=1):
    """Вызов Bot API. Из РФ ~половина соединений к Telegram виснет, отдавая по байту: timeout у urlopen это не ловит (23.10 заявка шла 40 мин).
    curl --max-time режет попытку целиком; новое соединение обычно проходит за 0,3 с. Токен — через stdin, не в ps. None — не дошло."""
    cfg = f'url = "https://api.telegram.org/bot{ENV["BOT_TOKEN"]}/{method}"\n'
    for _ in range(tries):
        r = subprocess.run(['curl', '-s', '--max-time', str(wait), '-K', '-', '-H', 'Content-Type: application/json', '--data-binary', json.dumps(body)],
                           input=cfg, capture_output=True, text=True)
        try:
            j = json.loads(r.stdout)
            if j.get('ok'):
                return j
        except ValueError:
            pass
        print(f'telegram {method}: curl rc={r.returncode} {r.stdout[:200]}', flush=True)
    return None


def send_tg(text):
    return tg('sendMessage', {'chat_id': ENV['CHAT_ID'], 'text': text, 'disable_web_page_preview': True}, tries=10) is not None


wake = threading.Event()


def tg_text(r):
    """Только номер, ниша, способ связи и ссылка. Имя, контакт и заметку в Telegram не шлём (152-ФЗ)."""
    calc = json.loads(r['calc'] or '{}')
    text = f"Новая заявка №{r['id']} · {NICHES[r['niche']]} · ответить: {VIA[r['via']]}" + (' · из чата' if r['source'] == 'chat' else '')
    if calc.get('risk'):
        text += f"\nПо калькулятору теряет {rub(calc['risk'])} в месяц"
    return text + f"\n{SITE}/admin/#lead-{r['id']}"


STATUSES = ('new', 'noanswer', 'contacted', 'meeting', 'done', 'noshow', 'lost')
HOUR = timedelta(hours=1)


def dt(s):
    return datetime.strptime(s, '%Y-%m-%d %H:%M')  # время в базе — МСК без зоны


def plan(r):
    """Напоминания по заявке: [(ключ, когда, текст)]. В Telegram — только номер, ниша, время и ссылка, без имени и контакта (152-ФЗ)."""
    n, link = f"№{r['id']} ({NICHES.get(r['niche'], r['niche'])})", f"\n{SITE}/admin/#lead-{r['id']}"
    if r['status'] == 'new':
        t = dt(r['ts'])
        return [('new5', t + timedelta(minutes=5), f'Заявка {n}: ты позвонил или нет? Отметь в админке.' + link),
                ('new10', t + timedelta(minutes=10), f'Заявка {n} всё ещё не отмечена. Позвонил?' + link),
                ('new15', t + timedelta(minutes=15), f'Прошло 15 минут, заявка {n} без ответа. Клиент остывает — позвони сейчас.' + link),
                ('new2h', t + 2 * HOUR, f'Заявка {n} висит 2 часа. Позвонил или отказ?' + link)]
    if r['status'] == 'noanswer' and r['acted']:  # ключ от времени нажатия: новое «Не дозвонился» — новый отсчёт 30 минут
        return [(f"na {r['acted']}", dt(r['acted']) + timedelta(minutes=30), f'Заявка {n}: попробуй дозвониться ещё раз. Получилось?' + link)]
    if r['status'] == 'meeting' and r['meet_at']:
        m, hm = dt(r['meet_at']), r['meet_at'][11:]
        return [('m24', m - 24 * HOUR, f'Завтра в {hm} диагностика по заявке {n} — отправь клиенту напоминание (текст в админке).' + link),
                ('m2', m - 2 * HOUR, f'Сегодня в {hm} диагностика по заявке {n} — отправь напоминание (текст в админке). Если не ответит — позвони.' + link),
                ('after', m + timedelta(minutes=15), f'Диагностика №{r["id"]} прошла? Отметь в админке: проведена или не пришёл.' + link)]
    return []


def digest(now):
    """Утренняя сводка: диагностики сегодня и заявки без движения больше суток. Только номера и время. None — сказать нечего."""
    old, fresh = ((now - timedelta(days=k)).strftime('%Y-%m-%d %H:%M') for k in (1, 30))  # ponytail: старше 30 дней не напоминаем — забытые тестовые заявки не висят вечно
    with db() as c:
        meets = c.execute("select id, meet_at from leads where status = 'meeting' and meet_at like ? order by meet_at", (now.strftime('%Y-%m-%d') + '%',)).fetchall()
        stuck = c.execute("select id, status from leads where status in ('new', 'noanswer', 'contacted') and ts < ? and ts >= ? order by id", (old, fresh)).fetchall()
    lines = [f'Диагностики сегодня: ' + ', '.join(f"{r['meet_at'][11:]} — №{r['id']}" for r in meets)] if meets else []
    for st, title in (('new', 'Не обработаны больше суток'), ('noanswer', 'Не дозвонился больше суток'), ('contacted', 'Связался, но диагностики нет')):
        if ids := [f"№{r['id']}" for r in stuck if r['status'] == st]:
            lines.append(f'{title}: ' + ', '.join(ids))
    return '\n'.join(['Доброе утро! План на сегодня:', *lines, f'{SITE}/admin/']) if lines else None


def remind(now=None):
    """Наступившие напоминания — каждое один раз. Опоздавшие больше чем на час (сервер лежал) только помечаем. False — Telegram не ответил."""
    now, ok = now or datetime.now(), True
    with db() as c:
        rows = c.execute("select id, ts, niche, status, meet_at, rem, acted from leads where status in ('new', 'noanswer', 'meeting')").fetchall()
    for r in rows:
        try:
            items, rem = plan(r), json.loads(r['rem'] or '[]')
        except ValueError as e:  # кривое время в одной заявке не должно останавливать остальные
            print(f'remind №{r["id"]}: {e!r}', flush=True)
            continue
        for key, at, text in items:
            if key in rem or at > now:
                continue
            if now - at <= HOUR and not send_tg(text):
                ok = False
                continue
            rem.append(key)
            with db() as c:  # время или статус успели поменять в админке — не затираем их новый rem
                c.execute('update leads set rem = ? where id = ? and status = ? and meet_at is ?', (json.dumps(rem), r['id'], r['status'], r['meet_at']))
    day, at = now.strftime('%Y-%m-%d'), now.replace(hour=9, minute=30, second=0, microsecond=0)
    if at <= now <= at + HOUR:
        with db() as c:
            done = c.execute('select 1 from digests where day = ?', (day,)).fetchone()
        if not done:
            text = digest(now)
            if not text or send_tg(text):
                with db() as c:
                    c.execute('insert or ignore into digests(day) values (?)', (day,))
            else:
                ok = False
    return ok


def meet_time(v, now):
    """'YYYY-MM-DD HH:MM' МСК с 10:00 до 18:00 и не в прошлом. None — не годится."""
    try:
        m = dt(v)
        assert re.fullmatch(r'\d{4}-\d\d-\d\d \d\d:\d\d', v) and '10:00' <= v[11:] <= '18:00' and m > now
        return v
    except Exception:
        return None


def lead_action(d, now=None):
    """Действие из админки: (код, текст ошибки или '')."""
    now = now or datetime.now()
    lid = d.get('id')
    with db() as c:
        r = c.execute('select * from leads where id = ?', (lid,)).fetchone() if type(lid) is int else None
    if not r:
        return 404, 'Нет такой заявки'
    a, stamp = d.get('action'), now.strftime('%Y-%m-%d %H:%M')
    if a == 'set_status':
        if d.get('status') not in STATUSES or (d['status'] in ('meeting', 'noshow', 'done') and not r['meet_at']):
            return 400, 'Такой статус сейчас поставить нельзя'
        up = {'status': d['status']} | ({'acted': stamp} if d['status'] == 'noanswer' else {})  # от acted — повтор через 30 минут
    elif a in ('set_meeting', 'reschedule'):
        m = meet_time(d.get('meet_at'), now)
        if not m:
            return 400, 'Время — с 10:00 до 18:00 МСК и не в прошлом'
        # напоминания, чьё время уже прошло (встреча меньше чем через сутки), сразу помечаем отправленными
        up = {'status': 'meeting', 'meet_at': m, 'acted': stamp,
              'rem': json.dumps([k for k, at, _ in plan(dict(r) | {'status': 'meeting', 'meet_at': m}) if at <= now])}
        if a == 'set_meeting':
            link = clean(d.get('meet_link'), 300)
            if link and not re.fullmatch(r'https?://\S+', link):
                return 400, 'Ссылка на встречу должна начинаться с https://'
            up |= {'meet_link': link, 'last': 'meeting'}
        elif r['status'] != 'meeting':
            return 400, 'Переносить можно только назначенную диагностику'
        else:
            up |= {'reschedules': (r['reschedules'] or 0) + 1, 'last': 'early' if d.get('early') is True else 'late'}
    elif a in ('noshow', 'done'):
        if r['status'] != 'meeting':
            return 400, 'Сначала назначьте диагностику'
        up = {'status': 'done'} if a == 'done' else {'status': 'noshow', 'noshows': (r['noshows'] or 0) + 1, 'last': 'noshow', 'acted': stamp}
    elif a == 'lost':
        up = {'status': 'lost'}
    elif a == 'comment' and isinstance(d.get('text'), str):
        up = {'comment': clean(d['text'], 2000)}
    else:
        return 400, 'Непонятное действие'
    with db() as c:
        c.execute(f"update leads set {', '.join(k + ' = ?' for k in up)} where id = ?", (*up.values(), lid))
    return 200, ''


def tpl(r, now):
    """Какой текст клиенту показать (сами тексты по регламенту — в admin/src/App.tsx). '' — никакой."""
    if r['status'] == 'noshow':
        return 'noshow2' if (r['noshows'] or 0) >= 2 else 'noshow1'
    if r['status'] != 'meeting' or not r['meet_at'] or now > dt(r['meet_at']):
        return ''
    m, last = dt(r['meet_at']), r['last']
    by_last = f"{last}{min(r['reschedules'] or 1, 2)}" if last in ('early', 'late') else 'qualify'
    if (r['acted'] and now - dt(r['acted']) < HOUR) or now < m - 24 * HOUR:  # только что назначили или перенесли — текст про это
        return by_last
    return 'h2' if now >= m - 2 * HOUR else 'day'


def notify_loop():
    # Telegram из РФ режется по IP: часть запросов висит, поэтому повторяем, пока не дойдёт.
    purged = 0
    while True:
        failed = False
        try:  # поток не должен умирать от одной ошибки базы: 12.10 он упал, и уведомления встали
            if time.time() - purged > 86400:  # при старте и раз в сутки
                purge()
                purged = time.time()
            with db() as c:
                rows = c.execute('select id, niche, via, calc, source from leads where notified = 0 order by id').fetchall()
            for r in rows:
                if send_tg(tg_text(r)):
                    with db() as c:
                        c.execute('update leads set notified = 1 where id = ?', (r['id'],))
                else:
                    failed = True
            if not remind():
                failed = True
        except Exception as e:
            print(f'notify: {e!r}', flush=True)
            failed = True
        wake.wait(5 if failed else 20)
        wake.clear()


def spend_line():
    """Траты на нейросеть сегодня. Файл голоса лежит в чужом StateDirectory (DynamicUser), отсюда не читается — спрашиваем сам voice.py."""
    s = f'чат {DEMO.spend.cap - DEMO.spend.left():.1f} из {DEMO.spend.cap:g} ₽'
    try:
        with urllib.request.urlopen('http://127.0.0.1:8082/spend', timeout=2) as r:
            v = json.load(r)
        return s + f", голос {v['rub']:.1f} из {v['cap']:g} ₽"
    except Exception:
        return s + ', голос — нет данных'


COOKIE_AGE = 30 * 86400


def sign(exp):
    key = hmac.new(ENV['ADMIN_KEY'].encode(), b'admin-cookie', hashlib.sha256).digest()  # ключ куки выведен из ADMIN_KEY
    return f'{exp}.' + hmac.new(key, str(exp).encode(), hashlib.sha256).hexdigest()


def cookie_ok(header):
    """Кука adm=<срок>.<подпись>: подпись наша и срок не вышел."""
    for part in (header or '').split(';'):
        k, _, v = part.strip().partition('=')
        if k == 'adm' and v.split('.')[0].isdigit():
            return hmac.compare_digest(sign(int(v.split('.')[0])), v) and int(v.split('.')[0]) > time.time()
    return False


def admin_data(now=None):
    """Заявки, диалоги и что сделать сегодня — для /admin/ одним JSON."""
    now = now or datetime.now()
    dl = dialogs()
    with db() as c:
        rows = c.execute('select * from leads order by id desc limit 200').fetchall()
    turns = lambda ts: [{'role': t['role'], 'kind': t['kind'], 'text': t['text']} for t in ts]
    leads = [{k: r[k] for k in ('id', 'ts', 'niche', 'name', 'via', 'contact', 'source', 'note', 'sid', 'page', 'status', 'meet_at', 'meet_link',
                                'reschedules', 'noshows', 'comment')}
             | {'calc': json.loads(r['calc'] or '{}'), 'turns': turns(dl.get(r['sid'], []) if r['sid'] else []), 'tpl': tpl(r, now)} for r in rows]
    today, tomorrow = now.strftime('%Y-%m-%d'), (now + timedelta(days=1)).strftime('%Y-%m-%d')
    meets = sorted((r for r in rows if r['status'] == 'meeting' and r['meet_at']), key=lambda r: r['meet_at'])
    todo = {'outcome': [r['id'] for r in meets if dt(r['meet_at']) <= now],  # встреча прошла, итог не отмечен
            'new': [r['id'] for r in rows if r['status'] in ('new', 'noanswer')],
            'today': [r['id'] for r in meets if r['meet_at'][:10] == today and dt(r['meet_at']) > now],
            'tomorrow': [r['id'] for r in meets if r['meet_at'][:10] == tomorrow]}
    with_lead = {r['sid'] for r in rows if r['sid']}
    ss = sorted(dl.items(), key=lambda kv: -kv[1][-1]['id'])[:200]  # новые сверху
    return {'spend': spend_line(), 'niches': NICHES, 'leads': leads, 'todo': todo,
            'dialogs': [{'sid': sid, 'start': ts[0]['ts'][:16], 'niche': ts[0]['niche'], 'kinds': list(dict.fromkeys(t['kind'] for t in ts)),
                         'turns': turns(ts), 'has_lead': sid in with_lead} for sid, ts in ss]}


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body, ctype='application/json', cookie=None):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    def ip(self):
        return self.headers.get('X-Forwarded-For', self.client_address[0]).split(',')[0].strip()

    def body(self, limit):
        n = int(self.headers.get('Content-Length', 0))
        if not 0 < n <= limit:
            raise ValueError('size')
        return self.rfile.read(n)

    def demo(self, path):
        if path != '/api/demo/chat':  # stt/tts убраны: сайт их не зовёт, а тратили бы ₽ мимо лимита
            return self.reply(404, '{}')
        if limited(self.ip(), 'chat', 40):
            return self.reply(429, BUSY)
        try:
            d = json.loads(self.body(20_000))
            niche = d.get('niche') if d.get('niche') in NICHES else 'general'
            hist = [{'role': m.get('role'), 'text': clean(m.get('text'), 500)} for m in d.get('history', [])[-16:] if isinstance(m, dict)]
            if not hist or hist[-1]['role'] != 'user' or not hist[-1]['text']:
                return self.reply(400, '{}')
            try:
                answer = DEMO.chat(niche, hist)
            except urllib.error.HTTPError as e:  # 5xx у Яндекса (сбой модели) — один повтор на запасной модели
                fb = ENV.get('YC_CHAT_FALLBACK', 'yandexgpt-5.1/latest')
                if e.code < 500 or fb == DEMO.model:
                    raise
                print(f'demo chat: {e!r}, повтор на {fb}', flush=True)
                answer = DEMO.chat(niche, hist, fb)
            if answer is None:  # лимит ₽ на сегодня выбран
                return self.reply(429, BUSY)
            text, lead, chips = parse_reply(answer, hist, niche)
            if sid := sid_of(d):
                try:
                    save_turns(sid, niche, 'chat', [('user', hist[-1]['text']), ('bot', text)])
                except Exception as e:  # ответ человеку важнее записи
                    print(f'dialog: {e!r}', flush=True)
            return self.reply(200, json.dumps({'text': text, **({'lead': lead} if lead else {}), **({'chips': chips} if chips else {})}, ensure_ascii=False))
        except Exception as e:
            print(f'demo chat: {e!r}', flush=True)
            return self.reply(502, json.dumps({'error': 'ИИ-менеджер сейчас недоступен. Оставьте заявку в форме ниже — Станислав ответит сам.'}, ensure_ascii=False))

    def internal(self):
        """Текст звонка от voice.py. Caddy пускает к нам только /api/* и всегда ставит X-Forwarded-For — с ним это запрос снаружи."""
        if 'X-Forwarded-For' in self.headers:
            return self.reply(403, '{}')
        try:
            d = json.loads(self.body(200_000))
            sid = sid_of(d)
            turns = [(t['role'], clean(t['text'], 2000)) for t in d['turns'][:300] if t.get('role') in ('user', 'bot') and str(t.get('text') or '').strip()]
            assert sid
        except Exception:
            return self.reply(400, '{}')
        save_turns(sid, d['niche'] if d.get('niche') in NICHES else 'general', 'voice', turns)
        self.reply(200, '{"ok":true}')

    def login(self):
        if limited(self.ip(), 'login', 10):
            return self.reply(429, '{"ok":false}')
        try:
            pw = json.loads(self.body(1000))['password']
            assert isinstance(pw, str)
        except Exception:
            return self.reply(400, '{"ok":false}')
        if not pw_ok(pw, ENV.get('ADMIN_PASS', '')):
            return self.reply(401, '{"ok":false}')
        exp = int(time.time()) + COOKIE_AGE
        self.reply(200, '{"ok":true}', cookie=f'adm={sign(exp)}; HttpOnly; Secure; SameSite=Strict; Path=/api/admin; Max-Age={COOKIE_AGE}')

    def do_POST(self):
        if self.path.startswith('/api/demo/'):
            return self.demo(self.path)
        if self.path == '/internal/dialog':
            return self.internal()
        if self.path == '/api/admin/login':
            return self.login()
        if self.path == '/api/admin/logout':
            return self.reply(200, '{"ok":true}', cookie='adm=; HttpOnly; Secure; SameSite=Strict; Path=/api/admin; Max-Age=0')
        if self.path == '/api/admin/lead':
            if not cookie_ok(self.headers.get('Cookie')):
                return self.reply(401, '{}')
            try:
                d = json.loads(self.body(5000))
                assert isinstance(d, dict)
            except Exception:
                return self.reply(400, '{"ok":false}')
            code, err = lead_action(d)
            return self.reply(code, json.dumps({'ok': True} if code == 200 else {'ok': False, 'error': err}, ensure_ascii=False))
        if self.path != '/api/lead':
            return self.reply(404, '{}')
        ip = self.ip()
        try:
            n = int(self.headers.get('Content-Length', 0))
            assert 0 < n < 4096
            d = json.loads(self.rfile.read(n))
            assert isinstance(d, dict)
        except Exception:
            return self.reply(400, '{"ok":false}')
        if d.get('website'):  # скрытое поле: его заполняют только боты
            return self.reply(200, '{"ok":true}')
        lead = lead_fields(d)
        if not lead or d.get('consent') is not True:
            return self.reply(400, '{"ok":false}')
        if limited(ip):
            return self.reply(429, '{"ok":false}')
        raw = d.get('calc') if isinstance(d.get('calc'), dict) else {}
        calc = {k: raw[k] for k in CALC_KEYS if isinstance(raw.get(k), (int, float)) and 0 <= raw[k] < 10**12}
        niche = d['niche'] if d.get('niche') in NICHES else 'general'
        with db() as c:
            c.execute('insert into leads(ts, name, via, contact, niche, calc, page, ref, ip, ua, source, note, sid) values (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (time.strftime('%Y-%m-%d %H:%M'), lead['name'], lead['via'], lead['contact'], niche, json.dumps(calc),
                       clean(d.get('page'), 500), clean(d.get('ref'), 500), ip, clean(self.headers.get('User-Agent'), 300),
                       'chat' if d.get('source') == 'chat' else 'form', clean(d.get('note'), 500), sid_of(d)))
        wake.set()
        self.reply(200, '{"ok":true}')

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/api/demo/hello':
            n = parse_qs(u.query).get('n', ['general'])[0]
            return self.reply(200, json.dumps({'text': GREETINGS.get(n, GREETINGS['general'])}, ensure_ascii=False))
        if u.path != '/api/admin/data':
            return self.reply(404, 'not found', 'text/plain')
        if not cookie_ok(self.headers.get('Cookie')):
            return self.reply(401, '{}')
        self.reply(200, json.dumps(admin_data(), ensure_ascii=False))

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    os.environ['TZ'] = 'Europe/Moscow'
    time.tzset()
    threading.Thread(target=notify_loop, daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1', 8081), Handler).serve_forever()
