// Scroll drives the story: step index shows/hides [data-show="from-to"] elements; hero progress pulls bubbles to centre.
// Reads data-show attributes only; no network, no storage.
(() => {
  const root = document.documentElement;
  const story = document.querySelector('.story');
  const hero = document.querySelector('.hero');
  const parts = [...document.querySelectorAll('[data-show]')].map(el => {
    const [a, b] = el.dataset.show.split('-');
    return { el, from: +a, to: b === '' ? Infinity : +b, msg: el.classList.contains('msg') };
  });
  const foots = ['Planning starts once one more person is in', 'Planning started · 0 of 2 shared times',
    'Provisional · 2 of 2 replied', 'Provisional · 2 of 2 replied', 'Provisional · 3 of 4 replied',
    'Both activities locked', 'Golf booked'];
  const foot = document.querySelector('[data-foot]');
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  let step = -1;

  function apply(s) {
    const keep = matchMedia('(max-width: 860px)').matches ? 2 : 3;
    parts.forEach(p => {
      const on = s >= p.from && s <= p.to;
      p.el.classList.toggle('on', on);
      if (p.msg) p.el.classList.toggle('old', on && s - p.from >= keep);
    });
    foot.textContent = foots[s];
  }

  function frame() {
    const hp = clamp(scrollY / (hero.offsetHeight * 0.75), 0, 1);
    root.style.setProperty('--hp', hp.toFixed(3));
    const r = story.getBoundingClientRect();
    const total = story.offsetHeight - innerHeight;
    const s = clamp(Math.floor(clamp(-r.top / total, 0, 1) * 7), 0, 6);
    if (s !== step) { step = s; apply(s); }
  }

  let queued = false;
  const tick = () => { if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; frame(); }); } };
  addEventListener('scroll', tick, { passive: true });
  addEventListener('resize', () => { step = -1; tick(); });
  frame();
})();
