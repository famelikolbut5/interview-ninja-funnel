const telegram = window.Telegram?.WebApp;
const statusBox = document.querySelector('#status');
const report = document.querySelector('#report');
const connection = document.querySelector('#connection');
const refreshDelay = 5 * 60 * 1000;
const number = new Intl.NumberFormat('ru-RU');
const percent = new Intl.NumberFormat('ru-RU', { style: 'percent', maximumFractionDigits: 1 });
const clock = new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit' });
const demoToggle = document.querySelector('#demo-toggle');
let demoMode = new URLSearchParams(window.location.search).get('demo') === '1';
let liveStats;

function displayStats() {
  if (!liveStats) return;
  const demoSteps = [
    { sent: 774 },
    { sent: 741 },
  ];
  const data = demoMode ? {
    ...liveStats,
    started: 1248,
    registered: 786,
    steps: liveStats.steps.map((step, index) => ({ ...step, ...demoSteps[index] })),
  } : liveStats;
  renderStats(data);
  demoToggle.textContent = demoMode ? 'Мои данные' : 'Показать демо';
  demoToggle.setAttribute('aria-pressed', String(demoMode));
}

demoToggle.addEventListener('click', () => {
  demoMode = !demoMode;
  const url = new URL(window.location.href);
  if (demoMode) url.searchParams.set('demo', '1');
  else url.searchParams.delete('demo');
  window.history.replaceState(null, '', url);
  displayStats();
});

function syncTheme() {
  const theme = telegram?.colorScheme ?? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  document.documentElement.dataset.theme = theme;
  telegram?.setHeaderColor?.(theme === 'dark' ? '#19191b' : '#f3f1ed');
  telegram?.setBackgroundColor?.(theme === 'dark' ? '#19191b' : '#f3f1ed');
}

function showStatus(title, text, requestId = '') {
  document.querySelector('#status-title').textContent = title;
  document.querySelector('#status-text').textContent = text;
  const code = document.querySelector('#error-id');
  code.textContent = requestId ? `Код ошибки ${requestId}` : '';
  code.hidden = !requestId;
  statusBox.hidden = false;
}

function renderStats(data) {
  const conversion = data.started ? data.registered / data.started : 0;
  document.querySelector('#started').textContent = number.format(data.started);
  document.querySelector('#registered').textContent = number.format(data.registered);
  document.querySelector('#conversion').textContent = percent.format(conversion);
  document.querySelector('#ring-value').style.strokeDasharray = `${100 * conversion} 100`;
  document.querySelector('#started-bar').style.width = data.started ? '100%' : '0';
  document.querySelector('#registered-bar').style.width = `${100 * conversion}%`;
  document.querySelector('#empty').hidden = data.started !== 0;
  const template = document.querySelector('#step-template');
  const steps = data.steps.map((step) => {
    const row = template.content.cloneNode(true);
    row.querySelector('.step-time').textContent = step.delay_seconds % 60 === 0
      ? `+${step.delay_seconds / 60} мин` : `+${step.delay_seconds} с`;
    row.querySelector('.step-title').textContent = step.title;
    row.querySelector('.step-result').textContent = number.format(step.sent);
    return row;
  });
  document.querySelector('#steps').replaceChildren(...steps);
  const updated = document.querySelector('#checked-at');
  updated.dateTime = data.checked_at;
  updated.textContent = demoMode ? 'Демонстрационные данные' : `Обновлено в ${clock.format(new Date(data.checked_at))}`;
  report.hidden = false;
  statusBox.hidden = true;
  connection.textContent = demoMode ? 'Демо' : 'На связи';
  connection.dataset.state = 'ready';
}

async function loadStats() {
  try {
    const response = await fetch('/api/stats', {
      headers: { 'X-Telegram-Init-Data': telegram.initData }, cache: 'no-store', signal: AbortSignal.timeout(8000),
    });
    if (response.status === 401 || response.status === 403) {
      report.hidden = true;
      connection.textContent = 'Нет доступа';
      connection.dataset.state = 'error';
      showStatus(response.status === 401 ? 'Открой статистику заново' : 'Доступ для владельца',
        response.status === 401 ? 'Сессия закончилась. Закрой это окно и нажми "Статистика" в чате бота.'
        : 'Войди в Telegram с аккаунта владельца бота.');
      return;
    }
    if (!response.ok) {
      connection.textContent = 'Нет связи';
      connection.dataset.state = 'error';
      showStatus('Не удалось обновить данные', 'Повторим через 5 минут. Если цифры уже загружены, они останутся на экране.', response.headers.get('X-Request-ID'));
    } else {
      liveStats = await response.json();
      displayStats();
    }
  } catch {
    connection.textContent = 'Нет связи';
    connection.dataset.state = 'error';
    showStatus('Связь прервалась', 'Проверь интернет. Попробуем снова через 5 минут.');
  }
  window.setTimeout(loadStats, refreshDelay);
}

syncTheme();
telegram?.onEvent?.('themeChanged', syncTheme);
telegram?.ready?.();
if (telegram?.initData) {
  telegram.expand?.();
  loadStats();
} else {
  connection.textContent = 'Вход через Telegram';
  showStatus('Открой из чата бота', 'Нажми "Статистика" в меню бота. Ещё можно отправить /stats.');
}
