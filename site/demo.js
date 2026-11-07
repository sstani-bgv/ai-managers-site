// ИИ-менеджер Станислава: чат (/api/demo/{hello,chat}, YandexGPT) с заявкой на разбор и голосовой звонок (/api/voice, Yandex Realtime API).
(() => {
  const $ = id => document.getElementById(id);
  // Номер разговора на эту загрузку страницы: по нему сервер собирает переписку и звонок и прикладывает к заявке (sendLead в index.html)
  const sid = window.demoSid = crypto.randomUUID ? crypto.randomUUID() // randomUUID есть только на https и localhost
    : '10000000-1000-4000-8000-100000000000'.replace(/[018]/g, c => (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16));
  const msgs = $('demoMsgs'), form = $('demoForm'), input = $('demoInput'), callBtn = $('demoCall'), status = $('demoStatus');
  if (!msgs || !form) return;
  const niche = document.documentElement.dataset.n || 'general';
  const history = [], used = {};
  let busy = false;

  const goal = g => { if (!used[g]) { used[g] = true; try { window.ym && ym(113550666, 'reachGoal', g); } catch {} } };
  const setStatus = t => { if (status && status.textContent !== (t || '')) status.textContent = t || ''; }; // без повторов: экранный диктор не перечитывает одно и то же
  const say = (role, text) => {
    const d = document.createElement('div');
    d.className = 'demo-msg ' + (role === 'user' ? 'user' : 'bot');
    d.textContent = text;
    msgs.append(d);
    msgs.scrollTop = msgs.scrollHeight;
    return d;
  };

  fetch('/api/demo/hello?n=' + niche).then(r => r.json()).then(j => { if (j.text && !history.length) { msgs.innerHTML = ''; say('bot', j.text); } }).catch(() => {});

  async function post(path, body, type = 'application/json') {
    const r = await fetch('/api/demo/' + path, {method: 'POST', headers: {'Content-Type': type}, body});
    if (!r.ok) {
      let e = 'Не получилось ответить. Попробуйте ещё раз.';
      try { e = (await r.json()).error || e; } catch {}
      throw new Error(e);
    }
    return r;
  }

  async function ask(text) {
    text = text.trim();
    if (busy || !text) return;
    busy = true;
    form.classList.add('busy');
    msgs.querySelectorAll('.demo-chips').forEach(c => c.remove()); // варианты ответа живут до следующей отправки
    history.push({role: 'user', text});
    say('user', text);
    goal('demo_chat');
    const typing = say('bot', 'Печатает…');
    typing.classList.add('typing');
    try {
      const j = await (await post('chat', JSON.stringify({niche, sid, history: history.slice(-15)}))).json(); // нечётное число: история начинается с реплики человека
      typing.remove();
      history.push({role: 'assistant', text: j.text});
      say('bot', j.text);
      // Неотправленную карточку меняем на новую, а без метки убираем, только если человек прислал цифры или @ (правил номер или ник).
      // Спросил о другом — карточка остаётся: модель не повторяет метку каждый ход.
      if (j.lead || /[\d@]/.test(text)) msgs.querySelectorAll('.demo-lead:not(.sent)').forEach(c => c.remove());
      if (j.lead) card(j.lead);
      else if (j.chips && !msgs.closest('.call')) chips(j.chips);
    } catch (e) {
      typing.remove();
      history.pop();
      setStatus(e.message);
    }
    busy = false;
    form.classList.remove('busy');
  }

  // Карточка заявки: ИИ собрал имя и контакт, но сохраняем, только когда человек сам отметит согласие и нажмёт кнопку
  const VIA = {phone: 'Телефон', telegram: 'Telegram', max: 'MAX'};
  const DONE = 'Готово! Станислав позвонит или напишет вам в тот же день. Если есть ещё вопросы, спрашивайте.';
  function card(lead) {
    const f = document.createElement('form');
    f.className = 'demo-lead';
    f.innerHTML = '<b>Заявка на бесплатный разбор</b><dl><dt>Имя</dt><dd></dd><dt></dt><dd></dd></dl>'
      + '<label class="check"><input type="checkbox" required><span>Даю <a href="soglasie.html" target="_blank">согласие на обработку персональных данных</a>. Подробнее — в <a href="politika.html" target="_blank">политике</a></span></label>'
      + '<button class="btn" type="submit">Отправить Станиславу</button>'
      + '<p class="err" hidden>Не получилось отправить. Попробуйте ещё раз через минуту.</p>'
      + '<p class="small">Что-то не так? Напишите в чат, я поправлю.</p>';
    const dd = f.querySelectorAll('dd');
    dd[0].textContent = lead.name;
    f.querySelectorAll('dt')[1].textContent = VIA[lead.via] || 'Контакт';
    dd[1].textContent = lead.contact;
    f.addEventListener('submit', async e => { // без галочки браузер сюда не пустит: required
      e.preventDefault();
      const btn = f.querySelector('button'), err = f.querySelector('.err');
      if (btn.disabled) return;
      btn.disabled = true;
      err.hidden = true;
      try {
        await window.sendLead({name: lead.name, via: lead.via, contact: lead.contact, consent: true, source: 'chat', note: lead.note || ''});
      } catch {
        err.hidden = false;
        btn.disabled = false;
        return;
      }
      f.classList.add('sent');
      f.querySelector('b').textContent = 'Заявка отправлена Станиславу';
      f.querySelectorAll('label, button, .small').forEach(x => x.remove());
      const last = history[history.length - 1]; // в истории реплики строго по очереди: дописываем к последнему ответу ИИ
      if (last && last.role === 'assistant') last.text += '\n' + DONE;
      say('bot', DONE);
    });
    msgs.append(f);
    msgs.scrollTop = msgs.scrollHeight;
  }

  // Варианты ответа от ИИ под последним сообщением: нажал — ушло как набранный текст
  function chips(list) {
    const d = document.createElement('div');
    d.className = 'demo-chips';
    for (const t of list) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = t;
      b.addEventListener('click', () => { setStatus(''); ask(t); });
      d.append(b);
    }
    msgs.append(d);
    msgs.scrollTop = msgs.scrollHeight;
  }

  form.addEventListener('submit', e => {
    e.preventDefault();
    if (busy) return; // не терять набранный текст, пока ждём ответ
    const t = input.value;
    input.value = '';
    setStatus('');
    ask(t);
  });
  document.querySelectorAll('.demo-q button').forEach(b => b.addEventListener('click', () => { setStatus(''); ask(b.textContent); })); // готовые вопросы — тот же путь, что и набранный текст

  // Голосовой звонок как по телефону: микрофон → /api/voice → Yandex Realtime API → голос обратно, перебивать можно.
  // Во время звонка вместо переписки — шар (без субтитров: распознанный текст пугал больше, чем помогал).
  const orb = $('demoOrb');
  if (!callBtn || !orb || !(navigator.mediaDevices && window.AudioWorkletNode && window.WebSocket)) return;
  callBtn.hidden = false;
  const RATE = 44100, label = callBtn.querySelector('span'), box = callBtn.closest('.demo'), who = box.querySelector('.demo-top').textContent;
  // Микрофон → PCM16 44,1 кГц кусками по 100 мс при любой частоте AudioContext (Firefox не смешивает частоты в одном графе).
  // ponytail: линейная интерполяция без фильтра — для речи хватает; если появится «металл», добавить ФНЧ перед прореживанием
  const WORKLET = `registerProcessor('mic', class extends AudioWorkletProcessor {
    constructor() { super(); this.step = sampleRate / ${RATE}; this.t = 0; this.p = 0; this.o = new Int16Array(${RATE / 10}); this.n = 0; }
    process([[x]]) {
      if (!x) return true;
      for (; this.t < x.length - 1; this.t += this.step) {
        const i = Math.floor(this.t), a = i < 0 ? this.p : x[i];
        this.o[this.n++] = Math.max(-1, Math.min(1, a + (x[i + 1] - a) * (this.t - i))) * 32767;
        if (this.n === ${RATE / 10}) { this.port.postMessage(this.o.buffer, [this.o.buffer]); this.o = new Int16Array(${RATE / 10}); this.n = 0; }
      }
      this.t -= x.length;
      this.p = x[x.length - 1];
      return true;
    }
  });`;
  let call = null;
  const b64 = buf => btoa(String.fromCharCode(...new Uint8Array(buf)));
  const send = (c, ev) => c.ws.readyState === 1 && c.ws.send(JSON.stringify(ev));
  const mode = (text, listening) => { setStatus(text); box.classList.toggle('listening', listening); };

  function hang(text) {
    const c = call;
    if (!c) return;
    call = null;
    if (c.ws) c.ws.close();
    if (c.stream) c.stream.getTracks().forEach(t => t.stop());
    c.ctx.close().catch(() => {});
    cancelAnimationFrame(c.raf);
    box.classList.remove('call');
    callBtn.classList.remove('on');
    label.textContent = 'Поговорить голосом';
    mode(text, false);
  }

  // Куски ответа встают в очередь вплотную друг к другу — без щелчков и пауз
  function play(c, ev) {
    const bin = atob(ev.delta), n = bin.length >> 1;
    if (!c.live || !n) return; // !live — хвост ответа, который перебили
    const buf = c.ctx.createBuffer(1, n, RATE), f = buf.getChannelData(0);
    for (let i = 0; i < n; i++) f[i] = ((bin.charCodeAt(2 * i) | bin.charCodeAt(2 * i + 1) << 8) << 16 >> 16) / 32768;
    const src = c.ctx.createBufferSource(), now = c.ctx.currentTime, at = c.playAt > now ? c.playAt : now + 0.1;
    src.buffer = buf;
    src.connect(c.out);
    src.start(at);
    if (ev.item_id !== c.item) { c.item = ev.item_id; c.itemAt = at; }
    c.playAt = at + buf.duration;
    c.srcs.push(src);
    src.onended = () => {
      c.srcs.splice(c.srcs.indexOf(src), 1);
      if (call === c && !c.srcs.length && !c.live) mode('Слушаю', true);
    };
    mode('Говорит ' + who, false);
  }

  // Человек заговорил — замолкаем сразу и говорим серверу, сколько он успел услышать
  function interrupt(c) {
    if (c.live) send(c, {type: 'response.cancel'});
    if (c.srcs.length) {
      const ms = Math.max(0, Math.round((c.ctx.currentTime - c.itemAt) * 1000));
      c.srcs.forEach(s => { s.onended = null; s.stop(); });
      c.srcs = [];
      c.playAt = 0;
      if (c.item) send(c, {type: 'conversation.item.truncate', item_id: c.item, audio_end_ms: ms});
    }
    c.live = false;
    mode('Слушаю', true);
  }

  function onEvent(c, ev) {
    switch (ev.type) {
      case 'response.created': c.live = true; break;
      case 'response.output_audio.delta': play(c, ev); break;
      case 'input_audio_buffer.speech_started': interrupt(c); break;
      case 'response.done': c.live = false; if (!c.srcs.length) mode('Слушаю', true); break;
      case 'end': hang(ev.text); break;
    }
  }

  // Шар (идея ElevenLabs Orb): мягкие пятна внутри крутятся быстрее от громкости ИИ, вокруг — волны голоса.
  // Пока ждём — медленно дышит, когда говорит человек — чуть откликается, когда ИИ — заметно волнуется.
  const calm = matchMedia('(prefers-reduced-motion: reduce)');
  const level = a => { // громкость 0…1 по последним ~10 мс звука
    if (!a) return 0;
    const d = a.buf ||= new Uint8Array(a.fftSize);
    a.getByteTimeDomainData(d);
    let s = 0;
    for (const v of d) s += (v - 128) * (v - 128);
    return Math.min(1, Math.sqrt(s / d.length) / 128 * 5);
  };
  function draw(c, now) {
    if (call !== c) return;
    c.raf = requestAnimationFrame(t => draw(c, t));
    const g = c.g, w = Math.round(orb.clientWidth * (devicePixelRatio || 1)), dt = Math.min(0.1, (now - (c.t || now)) / 1000);
    if (!w) return;
    if (orb.width !== w) orb.width = orb.height = w;
    c.t = now;
    const ease = (v, x) => v + (x - v) * (x > v ? 0.3 : 0.07); // быстро вверх, плавно вниз — без дрожи
    c.inL = ease(c.inL, level(c.inA));
    c.outL = ease(c.outL, level(c.outA));
    c.ph += calm.matches ? 0 : dt * (0.3 + c.outL * 2.4);
    const t = now / 1000, x = w / 2, R = w * 0.35;
    const k = calm.matches ? 1 + Math.max(c.outL, c.inL * 0.5) * 0.1 // без анимаций — только пульс размером
      : 1 + Math.sin(t * 1.7) * 0.025 * (1 - c.outL) + c.outL * 0.07 + c.inL * 0.025;
    const r = R * k, col = c.col;
    g.clearRect(0, 0, w, w);
    if (!calm.matches) { // волны вокруг: почти незаметны в тишине, расходятся, когда говорит ИИ
      const amp = R * (0.012 + c.outL * 0.15 + c.inL * 0.025);
      g.strokeStyle = col;
      g.lineWidth = w / 160;
      for (let j = 0; j < 3; j++) {
        g.globalAlpha = (0.38 - j * 0.11) * (0.35 + c.outL * 0.65 + c.inL * 0.2);
        g.beginPath();
        for (let i = 0; i <= 120; i++) {
          const a = i / 120 * Math.PI * 2;
          const rr = r * (1.07 + j * 0.075) + amp * (1 - j * 0.2) * (0.6 * Math.sin(3 * a + c.ph * 2 + j * 2.1) + 0.4 * Math.sin(5 * a - c.ph * 3 + j));
          g.lineTo(x + Math.cos(a) * rr, x + Math.sin(a) * rr);
        }
        g.stroke();
      }
      g.globalAlpha = 1;
    }
    const glow = g.createRadialGradient(x, x, r * 0.9, x, x, r * 1.35);
    glow.addColorStop(0, col + '33');
    glow.addColorStop(1, col + '00');
    g.fillStyle = glow;
    g.fillRect(0, 0, w, w);
    g.save();
    g.beginPath();
    g.arc(x, x, r, 0, Math.PI * 2);
    g.fillStyle = col;
    g.fill();
    g.clip();
    for (let i = 0; i < 3; i++) { // светлые облака внутри
      const a = c.ph + i * 2.1, bx = x + Math.cos(a) * r * 0.45, by = x + Math.sin(a * 1.3) * r * 0.4;
      const br = r * (0.6 + 0.15 * Math.sin(c.ph * 1.7 + i) + c.outL * 0.2);
      const b = g.createRadialGradient(bx, by, 0, bx, by, br);
      b.addColorStop(0, 'rgba(255,255,255,.42)');
      b.addColorStop(1, 'rgba(255,255,255,0)');
      g.fillStyle = b;
      g.fillRect(0, 0, w, w);
    }
    const shade = g.createRadialGradient(x - r * 0.3, x - r * 0.35, r * 0.1, x, x, r); // блик сверху слева, тень по краю — объём
    shade.addColorStop(0, 'rgba(255,255,255,.35)');
    shade.addColorStop(0.55, 'rgba(255,255,255,0)');
    shade.addColorStop(1, 'rgba(0,0,0,.22)');
    g.fillStyle = shade;
    g.fillRect(0, 0, w, w);
    g.restore();
  }

  callBtn.addEventListener('click', async () => {
    if (call) return hang('Звонок завершён.');
    const c = call = {ctx: new (window.AudioContext || window.webkitAudioContext)(), srcs: [], playAt: 0, item: null,
      g: orb.getContext('2d'), col: getComputedStyle(orb).getPropertyValue('--line').trim(), inL: 0, outL: 0, ph: 0};
    c.ctx.resume().catch(() => {}); // iOS: звук разрешён только внутри нажатия
    c.out = c.ctx.createGain(); // весь голос ИИ идёт через него — отсюда громкость для шара
    c.out.connect(c.ctx.destination);
    c.out.connect(c.outA = c.ctx.createAnalyser());
    callBtn.classList.add('on');
    label.textContent = 'Закончить разговор';
    box.classList.add('call');
    mode('Подключаюсь…', false);
    c.raf = requestAnimationFrame(t => draw(c, t));
    try {
      c.stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true}});
    } catch {
      if (call === c) hang('Нет доступа к микрофону. Разрешите его в браузере или напишите текстом.');
      return;
    }
    if (call !== c) return c.stream.getTracks().forEach(t => t.stop()); // положили трубку, пока браузер спрашивал разрешение
    try {
      await c.ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET], {type: 'text/javascript'})));
    } catch {
      if (call === c) hang('Голосовой звонок не запустился. Напишите вопрос текстом.');
      return;
    }
    if (call !== c) return;
    const ws = c.ws = new WebSocket(location.origin.replace(/^http/, 'ws') + '/api/voice?n=' + niche + '&sid=' + sid);
    ws.onopen = () => goal('demo_voice');
    ws.onmessage = e => call === c && onEvent(c, JSON.parse(e.data));
    ws.onclose = () => call === c && hang('Связь прервалась. Попробуйте ещё раз.');
    const node = new AudioWorkletNode(c.ctx, 'mic', {numberOfOutputs: 0});
    node.port.onmessage = e => send(c, {type: 'input_audio_buffer.append', audio: b64(e.data)});
    const mic = c.ctx.createMediaStreamSource(c.stream);
    mic.connect(node);
    mic.connect(c.inA = c.ctx.createAnalyser());
  });
  document.addEventListener('visibilitychange', () => document.hidden && hang('Звонок завершён.'));
})();
