// Scroll drives the page: hero bubbles fly into the plan card, then the pinned story steps through [data-show="from-to"].
// Reads data-show attributes and layout only; no network, no storage.
(() => {
  const root = document.documentElement;
  const story = document.querySelector('.story');
  const hero = document.querySelector('.hero');
  const card = document.querySelector('.card');
  const bar = document.querySelector('.bar');
  const bubbles = [...document.querySelectorAll('.b')];
  const dots = [...document.querySelectorAll('.dots button')];
  const parts = [...document.querySelectorAll('[data-show]')].map(el => {
    const [a, b] = el.dataset.show.split('-');
    return { el, from: +a, to: b === '' ? Infinity : +b };
  });
  const foots = ['Planning starts once one more person is in', 'Planning started · 0 of 2 shared times',
    'Provisional · 2 of 2 replied', 'Provisional · 2 of 2 replied', 'Provisional · 3 of 4 replied',
    'Both activities locked', 'Golf booked · dinner needs a volunteer'];
  const foot = document.querySelector('[data-foot]');
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const ease = t => t * t * (3 - 2 * t);
  let step = -1, rest = [];

  // bubble resting centres (the field is fixed, so offsets are viewport coordinates)
  function measure() { rest = bubbles.map(b => ({ x: b.offsetLeft + b.offsetWidth / 2, y: b.offsetTop + b.offsetHeight / 2 })); }

  function apply(s) {
    parts.forEach(p => p.el.classList.toggle('on', s >= p.from && s <= p.to));
    // small screens have room for two chat bubbles; drop the oldest
    const live = [...document.querySelectorAll('.msg.on')], max = matchMedia('(max-width: 860px)').matches ? 2 : 9;
    live.forEach((m, i) => m.classList.toggle('old', i < live.length - max));
    dots.forEach((d, i) => i === s ? d.setAttribute('aria-current', 'step') : d.removeAttribute('aria-current'));
    foot.textContent = foots[s];
  }

  function frame() {
    const p = clamp(scrollY / hero.offsetHeight, 0, 1);
    root.style.setProperty('--hp', p.toFixed(3));
    bar.classList.toggle('solid', scrollY > 40);

    const r = card.getBoundingClientRect();
    const tx = r.left + r.width / 2, ty = r.top + r.height / 2;
    bubbles.forEach((b, i) => {
      const q = ease(clamp((p - 0.05 - i * 0.015) / 0.8, 0, 1));
      const rot = parseFloat(b.style.getPropertyValue('--r'));
      b.style.transform = `translate(${(tx - rest[i].x) * q}px, ${(ty - rest[i].y) * q}px) rotate(${rot * (1 - q)}deg) scale(${1 - 0.7 * q})`;
      b.style.opacity = (0.58 * (1 - clamp((q - 0.55) / 0.45, 0, 1))).toFixed(3);
    });

    const sr = story.getBoundingClientRect();
    const total = story.offsetHeight - innerHeight;
    const s = clamp(Math.floor(clamp(-sr.top / total, 0, 1) * 7), 0, 6);
    if (s !== step) { step = s; apply(s); }
  }

  dots.forEach((d, i) => d.addEventListener('click', () => {
    const per = (story.offsetHeight - innerHeight) / 7;
    scrollTo({ top: story.offsetTop + per * (i + 0.5) });
  }));

  // click-to-load YouTube: nothing from YouTube loads until the viewer asks for it
  const yt = document.querySelector('[data-yt]');
  yt.querySelector('.play').addEventListener('click', e => {
    e.preventDefault();
    yt.innerHTML = `<iframe src="https://www.youtube-nocookie.com/embed/${yt.dataset.yt}?autoplay=1&rel=0&playsinline=1" title="PlanBot demo" allow="autoplay; encrypted-media; picture-in-picture; fullscreen" referrerpolicy="strict-origin-when-cross-origin" allowfullscreen></iframe>`;
    yt.querySelector('iframe').focus();
  });

  let queued = false;
  const tick = () => { if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; frame(); }); } };
  addEventListener('scroll', tick, { passive: true });
  addEventListener('resize', () => { measure(); step = -1; tick(); });
  measure();
  if (document.fonts) document.fonts.ready.then(() => { measure(); tick(); });
  frame();
})();
