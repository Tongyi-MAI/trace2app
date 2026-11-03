(function () {
  "use strict";

  var HOMES = [
    ["xiaohongshu", "Xiaohongshu"], ["bilibili", "Bilibili"], ["weibo", "Weibo"], ["toutiao", "Toutiao"],
    ["netease_music", "NetEase Cloud Music"], ["kugou", "Kugou Music"], ["ximalaya", "Ximalaya"],
    ["dingtalk", "DingTalk"], ["youdao_dictionary", "Youdao Dictionary"], ["damai", "Damai"],
    ["meituan", "Meituan"], ["kfc", "KFC"], ["dingdongmaicai", "Dingdong Maicai"], ["didi", "Didi"],
    ["railway12306", "Railway 12306"], ["xianyu", "Xianyu"], ["dewu", "Dewu"], ["58tc", "58Tongcheng"],
    ["autohome", "Autohome"], ["dongchedi", "Dongchedi"]
  ];

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

  // ---------- Home-screen gallery ----------
  var homes = $("homes");
  HOMES.forEach(function (h) {
    var d = el("div", "home");
    var img = el("img");
    img.src = "assets/img/homes/" + h[0] + ".webp";
    img.alt = h[1] + " generated-app home screen";
    img.loading = "lazy"; img.width = 420; img.height = 909;
    img.setAttribute("data-zoom", "");
    d.appendChild(img); d.appendChild(el("span", null, h[1]));
    homes.appendChild(d);
  });

  // ---------- Hero montage: pause/play, and stop decoding when off-screen ----------
  var vid = $("montage"), vidBtn = $("vid-toggle"), heroVideo = $("hero-video");
  if (vid && vidBtn) {
    var userPaused = false;
    function syncVidBtn() {
      heroVideo.classList.toggle("paused", vid.paused);
      vidBtn.setAttribute("aria-label", vid.paused ? "Play video" : "Pause video");
    }
    vidBtn.addEventListener("click", function () {
      if (vid.paused) { userPaused = false; vid.play().catch(syncVidBtn); } else { userPaused = true; vid.pause(); }
    });
    vid.addEventListener("play", syncVidBtn); vid.addEventListener("pause", syncVidBtn);
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(function (es) {
        es.forEach(function (e) {
          if (e.isIntersecting) { if (!userPaused) vid.play().catch(function () {}); }
          else if (!vid.paused) vid.pause();
        });
      }, { threshold: 0.15 }).observe(vid);
    }
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) { userPaused = true; vid.pause(); }
    syncVidBtn();
  }

  // ---------- Source trace: the whole recorded sequence at once ----------
  var ST = window.T2W_SOURCE_TRACE, stFrames = $("strace-frames");
  if (ST && stFrames) {
    var ACTION = { open: "Open app", click: "Click", drag: "Drag", terminal: "End" };
    var nFrames = ST.frames.length;
    $("strace-app").textContent = ST.app;
    $("strace-title").textContent = ST.title;
    $("strace-count").textContent = nFrames + " steps";
    $("strace-goal-en").textContent = ST.goal_en;
    $("strace-goal-zh").textContent = ST.goal_zh;
    $("strace-source").textContent = "Published source trace " + ST.source.task_id + ". Frames are the original marked " +
      "screenshots, byte-identical to the published trace; captions come from the trace rewrite. The last frame is the " +
      "terminal observation, stopped before payment.";
    ST.frames.forEach(function (f) {
      var li = el("li", "sframe" + (f.has_action_mark ? "" : " end"));
      var im = el("img");
      im.src = f.thumb; im.loading = "lazy"; im.decoding = "async"; im.width = f.width; im.height = f.height;
      im.setAttribute("data-zoom", ""); im.setAttribute("data-full", f.img);
      im.alt = ST.app + " source trace, step " + f.n + " of " + nFrames + (f.has_action_mark ? ", recorded action marked" : ", terminal observation");
      li.appendChild(im);
      var meta = el("div", "sframe-meta");
      meta.appendChild(el("span", "sframe-n", String(f.n)));
      meta.appendChild(el("span", "sframe-type", ACTION[f.type] || f.type));
      li.appendChild(meta);
      var cap = el("p", "sframe-cap", f.caption); cap.lang = "zh-CN";
      li.appendChild(cap);
      stFrames.appendChild(li);
    });
  }

  // ---------- Interactive trajectory viewer ----------
  var TR = window.T2W_CASES || [];
  var trTabs = $("traj-tabs");
  if (TR.length && trTabs) {
    var T = {
      img: $("traj-img"), steps: $("traj-steps"), prev: $("traj-prev"), next: $("traj-next"), play: $("traj-play"),
      playLbl: $("traj-play-lbl"), pos: $("traj-pos"), n: $("traj-n"), think: $("traj-think"),
      cps: $("traj-cps"), cpCount: $("traj-cp-count"), cpLegend: document.querySelector("#traj .cp-legend"), reward: $("traj-reward"),
      rewardRow: $("traj-reward-row"), markLabel: $("traj-mark-label"),
      thinkingLabel: $("traj-thinking-label"), verifyLabel: $("traj-verify-label"),
      verificationTime: $("traj-verification-time"),
      rewardNum: $("traj-reward-num"), app: $("traj-app"), title: $("traj-title"),
      goalEn: $("traj-goal-en"), goalZh: $("traj-goal-zh"), attempts: $("traj-attempts"), root: $("traj")
    };
    var ts = { c: 0, a: 0, s: 0, timer: null };

    function cur() { var c = TR[ts.c], a = c.attempts[ts.a]; return { c: c, a: a, st: a.steps[ts.s] }; }
    function preloadStep(i) { var a = cur().a; if (a.steps[i]) { var im = new Image(); im.src = a.steps[i].img; } }

    function renderStep() {
      var x = cur(), c = x.c, a = x.a, st = x.st, n = a.steps.length;
      T.img.classList.add("loading");
      T.img.onload = function () { T.img.classList.remove("loading"); };
      T.img.src = st.img;
      T.img.alt = c.app + ", step " + st.n + " of " + n + (st.has_action_mark ? ": marked action target" : ": terminal observation");
      T.n.textContent = st.n;
      var walkthrough = c.presentation === "walkthrough";
      $("traj-observation").textContent = !st.has_action_mark && st.type === "terminal" ? "Outcome observation" :
        st.display_origin === "publication_package_replay_after_reference_resource_fix" ? "Repaired package replay · before action" :
        st.fresh_observation ? "Observation before action" : "Last observation · step " + st.observation_step + " (no new screenshot)";
      T.markLabel.hidden = !st.has_action_mark;
      T.pos.textContent = "Step " + st.n + " / " + n;
      T.thinkingLabel.textContent = st.type === "terminal" ? "Outcome observation" : "Step rationale";
      T.think.textContent = st.think || "(no step rationale)";
      $("traj-verification").textContent = st.verification.note;
      T.verifyLabel.textContent = walkthrough ? "Task checks" : "Verification";
      T.verificationTime.textContent = walkthrough ? "Task checks established by this point in the sequence." :
        st.type === "terminal" ? "Post-action observation from the publication package." :
        "Checkpoint state after executing this step in the publication package; the marked image shows the preceding observation.";
      T.think.scrollTop = 0;

      var prevReached = ts.s > 0 ? a.steps[ts.s - 1].reached : [];
      var reached = {}; st.reached.forEach(function (id) { reached[id] = true; });
      var was = {}; prevReached.forEach(function (id) { was[id] = true; });
      T.cps.innerHTML = "";
      var hit = 0, newly = 0;
      c.checkpoints.forEach(function (cp) {
        var li = el("li", reached[cp.id] ? (was[cp.id] ? "hit" : "hit new") : (was[cp.id] ? "lost" : ""));
        var status = st.score === null && !walkthrough ? "Not sampled" : reached[cp.id] ? (was[cp.id] ? "Met" : "Newly met") : (was[cp.id] ? "Lost this step" : "Not met");
        li.setAttribute("aria-label", status + ": " + cp.label);
        li.title = status;
        li.appendChild(el("span", "cp-dot"));
        li.appendChild(el("span", "cp-lbl", cp.label));
        if (reached[cp.id] && !was[cp.id]) li.appendChild(el("span", "cp-change", "+"));
        if (!reached[cp.id] && was[cp.id]) li.appendChild(el("span", "cp-change", "−"));
        if (reached[cp.id]) hit++;
        if (reached[cp.id] && !was[cp.id]) newly++;
        T.cps.appendChild(li);
      });
      T.cpCount.textContent = st.score === null && !walkthrough ? "Not sampled" :
        c.checkpoints.length ? hit + " / " + c.checkpoints.length + (newly ? "  (+" + newly + " this step)" : "") : "Outcome only";
      T.cpLegend.style.display = c.checkpoints.length ? "" : "none";
      T.rewardRow.hidden = walkthrough;
      var pct = Math.round(100 * (st.score || 0));
      T.reward.style.width = pct + "%";
      T.reward.className = ts.s === n - 1 ? (a.success ? "done" : "partial") : "";
      T.rewardNum.textContent = st.score === null ? "—" : st.score.toFixed(2);

      Array.prototype.forEach.call(T.steps.children, function (b, i) {
        b.classList.toggle("active", i === ts.s);
        b.classList.toggle("seen", i < ts.s);
        if (i === ts.s) b.setAttribute("aria-current", "step"); else b.removeAttribute("aria-current");
      });
      var act = T.steps.children[ts.s];
      // Keep only the horizontal timeline in view; never scroll the whole document.
      if (act) T.steps.scrollLeft = act.offsetLeft - (T.steps.clientWidth - act.offsetWidth) / 2;
      T.prev.disabled = ts.s === 0;
      T.next.disabled = ts.s === n - 1;
      preloadStep(ts.s + 1);
    }

    function renderAttempt() {
      var x = cur(), a = x.a, c = x.c;
      T.steps.innerHTML = "";
      a.steps.forEach(function (st, i) {
        var b = el("button", "stepdot" + (["answer", "finished", "terminal"].indexOf(st.type) >= 0 ? " term" : ""), String(st.n));
        var gained = i === 0 ? st.reached.length : st.reached.length - a.steps[i - 1].reached.length;
        if (gained > 0) b.classList.add("gain");
        if (gained < 0) b.classList.add("loss");
        b.title = "Step " + st.n + (gained > 0 ? " (+" + gained + " checkpoint" + (gained > 1 ? "s" : "") + ")" : "");
        b.setAttribute("aria-label", b.title);
        b.addEventListener("click", function () { stopPlay(); ts.s = i; renderStep(); });
        T.steps.appendChild(b);
      });
      Array.prototype.forEach.call(T.attempts.children, function (b, i) { b.classList.toggle("active", i === ts.a); b.setAttribute("aria-pressed", String(i === ts.a)); });
      T.root.classList.toggle("fail", a.success === false);
      renderStep();
    }

    function renderCase() {
      var c = TR[ts.c];
      Array.prototype.forEach.call(trTabs.children, function (t, i) {
        t.classList.toggle("active", i === ts.c);
        t.setAttribute("aria-selected", i === ts.c ? "true" : "false");
        t.tabIndex = i === ts.c ? 0 : -1;
      });
      T.app.textContent = c.app;
      T.title.textContent = c.title;
      T.goalEn.textContent = c.goal_en;
      T.goalZh.textContent = c.goal_zh;
      $("traj-source").textContent = c.source_note;
      T.attempts.innerHTML = "";
      c.attempts.forEach(function (a, i) {
        var b = el("button", "attempt" + (a.success === true ? " ok" : a.success === false ? " ko" : ""), null);
        b.appendChild(el("span", "att-lbl", a.label));
        var actions = a.steps.filter(function (s) { return s.has_action_mark; }).length;
        b.appendChild(el("span", "att-score", a.score === null ? actions + " actions" : "eval " + a.score.toFixed(2)));
        b.addEventListener("click", function () { stopPlay(); ts.a = i; ts.s = 0; renderAttempt(); });
        T.attempts.appendChild(b);
      });
      T.attempts.hidden = false;
      renderAttempt();
    }

    function stopPlay() {
      if (ts.timer) { clearInterval(ts.timer); ts.timer = null; }
      T.play.classList.remove("on"); T.playLbl.textContent = "Play";
      T.play.setAttribute("aria-label", "Play trajectory");
      T.play.setAttribute("aria-pressed", "false");
    }
    function startPlay() {
      if (ts.s === cur().a.steps.length - 1) { ts.s = 0; renderStep(); }
      T.play.classList.add("on"); T.playLbl.textContent = "Pause";
      T.play.setAttribute("aria-label", "Pause trajectory");
      T.play.setAttribute("aria-pressed", "true");
      ts.timer = setInterval(function () {
        if (ts.s >= cur().a.steps.length - 1) { stopPlay(); return; }
        ts.s++; renderStep();
        if (ts.s === cur().a.steps.length - 1) stopPlay();
      }, 1800);
    }

    TR.forEach(function (c, i) {
      var b = el("button", "tab app", null);
      var ic = el("img"); ic.src = "assets/img/homes/" + c.key + ".webp"; ic.alt = ""; ic.loading = "lazy"; ic.width = 22; ic.height = 48;
      b.appendChild(ic); b.appendChild(el("span", null, c.tab_label));
      b.setAttribute("role", "tab");
      b.addEventListener("click", function () { stopPlay(); ts.c = i; ts.a = 0; ts.s = 0; renderCase(); });
      trTabs.appendChild(b);
    });
    T.prev.addEventListener("click", function () { stopPlay(); if (ts.s > 0) { ts.s--; renderStep(); } });
    T.next.addEventListener("click", function () { stopPlay(); if (ts.s < cur().a.steps.length - 1) { ts.s++; renderStep(); } });
    T.play.addEventListener("click", function () { if (ts.timer) stopPlay(); else startPlay(); });
    renderCase();
    trTabs.addEventListener("keydown", function (e) {
      var i = ts.c, last = TR.length - 1;
      if (e.key === "ArrowRight") i = i === last ? 0 : i + 1;
      else if (e.key === "ArrowLeft") i = i === 0 ? last : i - 1;
      else if (e.key === "Home") i = 0;
      else if (e.key === "End") i = last;
      else return;
      e.preventDefault(); trTabs.children[i].click(); trTabs.children[i].focus();
    });

    document.addEventListener("visibilitychange", function () { if (document.hidden) stopPlay(); });
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(function (entries) {
        if (!entries[0].isIntersecting) stopPlay();
      }).observe(T.root);
    }

    document.addEventListener("keydown", function (e) {
      if (lb && !lb.hidden) return;
      if (e.target.closest("input, textarea, select, [contenteditable='true'], [role='tablist']")) return;
      if (e.altKey || e.ctrlKey || e.metaKey) return;
      var r = T.root.getBoundingClientRect();
      if (r.top > window.innerHeight * 0.7 || r.bottom < window.innerHeight * 0.3) return;
      if (e.key === "ArrowLeft") { T.prev.click(); e.preventDefault(); }
      if (e.key === "ArrowRight") { T.next.click(); e.preventDefault(); }
      if (e.key === " " && e.target === document.body) { T.play.click(); e.preventDefault(); }
    });
  }

  // ---------- Lightbox ----------
  var lb = $("lightbox"), lbImg = $("lightbox-img");
  function closeLb() { lb.hidden = true; lbImg.src = ""; document.body.style.overflow = ""; }
  document.addEventListener("click", function (e) {
    var t = e.target;
    if (t.matches && t.matches("img[data-zoom]") && !lb.contains(t)) {
      lbImg.src = t.getAttribute("data-full") || t.currentSrc || t.src; lbImg.alt = t.alt;
      lb.hidden = false; lb.scrollTop = 0; document.body.style.overflow = "hidden";
    }
  });
  lb.addEventListener("click", closeLb);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !lb.hidden) closeLb(); });

  // ---------- Copy buttons ----------
  Array.prototype.forEach.call(document.querySelectorAll(".copy"), function (b) {
    b.addEventListener("click", function () {
      var text = $(b.getAttribute("data-copy")).innerText;
      var done = function () { b.textContent = "Copied"; setTimeout(function () { b.textContent = "Copy"; }, 1400); };
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done);
      else { var ta = el("textarea"); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove(); done(); }
    });
  });

  // ---------- Nav state, reveal-on-scroll, chart animation ----------
  var nav = $("topnav");
  var links = Array.prototype.slice.call(document.querySelectorAll(".navlinks a"));
  var sections = links.map(function (a) { return document.querySelector(a.getAttribute("href")); });
  function onScroll() {
    nav.classList.toggle("scrolled", window.scrollY > 8);
    var y = window.scrollY + 90, idx = -1;
    sections.forEach(function (s, i) { if (s && s.offsetTop <= y) idx = i; });
    links.forEach(function (a, i) { a.classList.toggle("active", i === idx); });
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  var targets = document.querySelectorAll(".card, .step, figure.wide, .two-col, .table-wrap, .chart, .bench-item, .case");
  if ("IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); } });
    }, { rootMargin: "0px 0px -8% 0px" });
    Array.prototype.forEach.call(targets, function (t) { t.classList.add("reveal"); io.observe(t); });
  } else {
    Array.prototype.forEach.call(targets, function (t) { t.classList.add("in"); });
  }
})();
