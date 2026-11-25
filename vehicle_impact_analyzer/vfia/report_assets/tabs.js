/* VFIA 보고서 탭: '보고서' / '기능 영향' / '영향 그래프'. 주소(#...)로 탭과 위치를 기억한다.
   #report, #feature, #feature:C003(기능 영향의 그 변경), #graph:C003:impact(그래프 스크립트가 처리), #C003(보고서 카드) */
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var hooks = {}, routes = {};
  function names() {
    return Array.prototype.map.call(document.querySelectorAll('nav.tabs a[data-tab]'), function (a) { return a.getAttribute('data-tab'); });
  }
  function show(name) {
    var list = names();
    if (list.indexOf(name) < 0) name = 'report';
    var wasHidden = $('tab-' + name) && $('tab-' + name).hidden;
    list.forEach(function (n) {
      var sec = $('tab-' + n), btn = $('tabbtn-' + n);
      if (sec) sec.hidden = n !== name;
      if (btn) btn.classList.toggle('on', n === name);
    });
    document.body.classList.toggle('ig-wide', name === 'graph');
    if (wasHidden && name !== 'report') {
      var t = document.querySelector('nav.tabs');
      if (t) t.scrollIntoView({ block: 'start' });
    }
    if (hooks[name]) hooks[name]();
  }
  function flash(el) {
    el.classList.add('flash');
    setTimeout(function () { el.classList.remove('flash'); }, 1800);
  }
  function reveal(el) {
    var d = el.closest('details');
    while (d) { d.open = true; d = d.parentElement && d.parentElement.closest('details'); }
    el.scrollIntoView({ block: 'start' });
    flash(el);
  }
  function route() {
    var h = decodeURIComponent(location.hash.replace(/^#/, ''));
    var parts = h.split(':'), head = parts[0];
    if (routes[head]) { show(head); routes[head](parts.slice(1)); return; }
    if (names().indexOf(head) >= 0) {
      show(head);
      if (head === 'feature' && parts[1]) {
        var el = $('fi-' + parts[1]);
        if (el) reveal(el);
      }
      return;
    }
    // 카드 id: 들어 있는 탭을 찾아 보여 준다
    var target = h ? $(h) : null;
    var sec = target ? target.closest('section[id^="tab-"]') : null;
    show(sec ? sec.id.slice(4) : 'report');
    if (target) reveal(target);
  }
  function go(id) {
    if (location.hash === '#' + id) { route(); } else { location.hash = id; }
  }
  Array.prototype.forEach.call(document.querySelectorAll('nav.tabs a[data-tab]'), function (a) {
    a.addEventListener('click', function (ev) {
      ev.preventDefault();
      var n = a.getAttribute('data-tab');
      var extra = window.vfiaTabs.hashFor[n] ? window.vfiaTabs.hashFor[n]() : '';
      history.pushState(null, '', '#' + n + extra);
      show(n);
      if (routes[n]) routes[n]([]);
    });
  });
  window.addEventListener('hashchange', route);
  window.vfiaTabs = { show: show, go: go, route: route, flash: flash, hashFor: {},
    onShow: function (n, f) { hooks[n] = f; }, onRoute: function (n, f) { routes[n] = f; } };
  document.addEventListener('DOMContentLoaded', route);
  if (document.readyState !== 'loading') setTimeout(route, 0);
})();
