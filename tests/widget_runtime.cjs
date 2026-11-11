// Deliberately small widget-profile DOM, not a browser implementation.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const docs = JSON.parse(fs.readFileSync(0, 'utf8'));
const decode = s => String(s).replace(/&(amp|lt|gt|quot|#39|#x27);/g,
  (_, k) => ({amp:'&',lt:'<',gt:'>',quot:'"','#39':"'",'#x27':"'"}[k]));
function harness(doc) {
  const ids = {}, buttons = {}, events = {}, timers = [];
  let requests = [];
  class Element {
    constructor(attrs = {}) {this.attrs=attrs;this.style={};this.handlers={};this.disabled=false;this.textContent='';this.classList={add:n=>{this.selected=n}};}
    set innerHTML(s) {
      this.markup=s;
      for (const k of this.childButtons || []) delete buttons[k];
      this.childButtons=[];
      parse(s, this);
    }
    get innerHTML() {return this.markup || '';}
    getAttribute(k) {return this.attrs[k] || null;}
    addEventListener(k, f) {(this.handlers[k] ||= []).push(f);}
    click() {if(!this.disabled) for (const f of this.handlers.click || []) f({target:this});}
  }
  function parse(html, owner) {
    for (const m of html.matchAll(/<([a-z]+)\b([^>]*)>/gi)) {
      const attrs={};for(const a of m[2].matchAll(/([\w-]+)="([^"]*)"/g)) attrs[a[1]]=decode(a[2]);
      const e=new Element(attrs);
      if(attrs.id) ids[attrs.id]=e;
      if(attrs['data-action']) {buttons[attrs['data-action']]=e;if(owner)owner.childButtons.push(attrs['data-action']);}
    }
  }
  parse(doc.html.replace(/<script>[\s\S]*?<\/script>/g,''));
  const W={chatData:JSON.stringify(doc.data),onEvent:(n,f)=>(events[n] ||= []).push(f),ready:()=>{},
    request:s=>{requests.push(s);return Promise.resolve({result:'{"ok":1,"again":1}'})}};
  const ctx=vm.createContext({window:{TelegramWidget:W},document:{getElementById:k=>ids[k],
    querySelector:q=>buttons[(q.match(/data-action="([^"]+)"/)||[])[1]]},
    setTimeout:f=>{timers.push(f)},setInterval:f=>{timers.push(f);return timers.length},clearInterval:()=>{},Date,console});
  for(const m of doc.html.matchAll(/<script>([\s\S]*?)<\/script>/g)) vm.runInContext(m[1],ctx);
  return {ids,buttons,requests,timers,update:s=>{W.chatData=JSON.stringify(s);for(const f of events.chatDataChanged || [])f();}};
}
const evil='<img src=x onerror="boom">&\'';
if (process.argv.includes('--chart')) {
  const rendered = docs.map(d => {const h=harness({html:d.html,data:{}});h.update(d.data);return h.ids.chart.innerHTML;});
  console.log(JSON.stringify(rendered));
} else (async()=>{
  let h=harness(docs.weather);
  h.update({...docs.weather.data,city:'Updated',h:[[evil,evil,0]]}); // even unvalidated state is escaped
  assert.equal(h.ids.ci.textContent,'Updated');
  assert.ok(!h.ids.hr.innerHTML.includes('<img'));
  assert.ok(h.ids.hr.innerHTML.includes('&lt;img'));
  h=harness(docs.tracker);
  h.update({...docs.tracker.data,t:'Updated',s:['One',evil,'Three'],i:1,n:'new note'});
  assert.equal(h.ids.t.textContent,'Updated');assert.equal(h.ids.n.textContent,'new note');
  assert.ok(h.ids.r.innerHTML.includes('&lt;img'));
  h=harness(docs.compare);
  let d=structuredClone(docs.compare.data);d.t='Updated';d.o[0].p='$999';d.o[0].n=evil;
  h.update(d);assert.equal(h.ids.h.textContent,'Updated');assert.ok(h.ids.g.innerHTML.includes('$999'));
  assert.ok(!h.ids.g.innerHTML.includes('<img'));
  h.buttons.a.click();assert.deepEqual(h.requests,['a:a']);await Promise.resolve();
  h.update(d);h.update(d);h.buttons.b.click();assert.deepEqual(h.requests,['a:a','a:b']);
  h.update({...d,chosen:'a'});assert.ok(h.buttons.a.disabled&&h.buttons.b.disabled);assert.equal(h.buttons.a.selected,'hte-chosen');
  h=harness(docs.approve);
  d={...docs.approve.data,t:'Updated',d:[['Recipient',evil]],risk:'new risk',exp:Date.now()/1000+60};
  h.update(d);assert.equal(h.ids.t.textContent,'Updated');assert.equal(h.ids.r.textContent,'new risk');
  assert.ok(h.ids.d.innerHTML.includes('&lt;img'));assert.ok(!h.ids.d.innerHTML.includes('<img'));
  h.buttons.ok.click();assert.equal(h.requests.length,0);
  h.update(d);h.buttons.ok.click();assert.equal(h.requests.length,0); // redraw cancels first-tap consent
  h.buttons.ok.click();assert.deepEqual(h.requests,['a:ok']);await Promise.resolve();
  h.update({...d,exp:1});assert.ok(h.buttons.ok.disabled&&h.buttons.no.disabled);
  assert.ok(h.ids.e.textContent.includes('已过期'));
  h.update({...d,chosen:'no'});assert.ok(h.ids.s.textContent.includes('拒绝'));assert.ok(h.buttons.ok.disabled);
  console.log('runtime regressions passed');
})().catch(e=>{console.error(e);process.exitCode=1});
