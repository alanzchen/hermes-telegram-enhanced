/* Synthetic renderer/layout contract; run with node tests/test_miniapp_js.cjs. */
const assert=require('node:assert/strict');
const layout=require('../static/layout.js');
const nodes=Array.from({length:7},(_,i)=>({id:String(i),label:'Node '+i,shape:i===1?'diamond':'rect'}));
const edges=[{from:'0',to:'1'},{from:'1',to:'2'},{from:'1',to:'3'},{from:'2',to:'4'},{from:'3',to:'4'},{from:'4',to:'5'},{from:'5',to:'6'}];
function nonOverlap(boxes){for(let i=0;i<boxes.length;i++)for(let j=i+1;j<boxes.length;j++){let a=boxes[i],b=boxes[j];assert.ok(a.x+a.w<=b.x||b.x+b.w<=a.x||a.y+a.h<=b.y||b.y+b.h<=a.y,`${a.id}/${b.id} overlap`);}}
for(const es of [edges,edges.concat({from:'6',to:'0'}),[]]){
 const a=layout.flowLayout(nodes,es); assert.deepEqual(a,layout.flowLayout(nodes,es)); nonOverlap(a.nodes);
 assert.equal(a.nodes.length,7); assert.ok(a.width>0&&a.height>0);
}
const tasks=Array.from({length:6},(_,i)=>({id:'t'+i,label:'Task '+i,start:'2026-11-01',end:'2026-11-03',group:i<3?'Design':'Build'}));
const gantt=layout.ganttLayout(tasks,'2026-11-02');assert.deepEqual(gantt,layout.ganttLayout(tasks,'2026-11-02'));nonOverlap(gantt.tasks);
assert.equal(gantt.tasks.length,6);assert.ok(Number.isFinite(gantt.todayX));

class Node {
 constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this.listeners={};this.dataset={};this.style={setProperty(){}};this.className='';this._text='';this.classList={add:()=>{},toggle:()=>{}};}
 set innerHTML(_value){throw Error('innerHTML is forbidden');}
 get textContent(){return this._text+this.children.map(x=>x.textContent).join('');}
 set textContent(value){this._text=String(value);this.children=[];}
 append(...nodes){this.children.push(...nodes);}
 appendChild(n){this.append(n);return n;}
 replaceChildren(...nodes){this.children=[];this._text='';this.append(...nodes);}
 setAttribute(k,v){this.attrs[k]=String(v);}
 addEventListener(k,v){this.listeners[k]=v;}
}
global.document={createElement:t=>new Node(t),createElementNS:(_ns,t)=>new Node(t),createTextNode:t=>{const n=new Node('#text');n.textContent=t;return n;}};
const renderer=require('../static/renderer.js');
const root=new Node('main');const evil='<img src=x onerror=alert(1)><script>bad()</script>';
renderer.render(root,{spec:{version:1,components:[{type:'heading',text:evil},{type:'text',text:'**'+evil+'**',markdown:true},{type:'table',columns:[evil],rows:[[evil]],sortable:true},{type:'choice',id:'pick',label:evil,options:[{value:'a',label:evil,details:evil}],action:{key:'pick',label:'Choose'}},{type:'flow',nodes:[{id:'x',label:evil}],edges:[]}]},results:{},closed:false},()=>{});
assert.ok(root.textContent.includes(evil));
function all(node){return [node,...node.children.flatMap(all)];}
assert.equal(all(root).filter(n=>['img','script'].includes(n.tagName)).length,0);
assert.equal(all(root).filter(n=>Object.keys(n.attrs).some(a=>a.startsWith('on'))).length,0);
assert.ok(all(root).some(n=>n.tagName==='strong'));
console.log('PASS: flow DAG/cycles/disconnected deterministic and nonoverlapping; Gantt deterministic and nonoverlapping; XSS text-only DOM rendering.');
const fs=require('node:fs'),vm=require('node:vm'),styleValues={};
const elements=new Map();
const style={setProperty:(k,v)=>styleValues[k]=v,removeProperty:k=>delete styleValues[k]};
const doc={getElementById:id=>{if(!elements.has(id))elements.set(id,new Node('div'));return elements.get(id);},documentElement:{dataset:{},style},addEventListener(){}};
const context={window:{Telegram:{WebApp:{initData:'',colorScheme:'light',themeParams:{bg_color:'#000000',text_color:'#ffffff',button_color:'#ffffff',button_text_color:'#ffffff'},ready(){},expand(){},onEvent(){}}}},document:doc,location:{search:''},URLSearchParams,matchMedia:()=>({matches:false}),getComputedStyle:()=>({getPropertyValue:k=>({'--surface':'#ffffff','--soft':'#e9f1ff','--muted':'#566477'}[k]||'')})};
vm.runInNewContext(fs.readFileSync(require.resolve('../static/app.js'),'utf8'),context);
assert.equal(styleValues['--openui-safe-background'],undefined,'hostile text must not be copied onto the fallback card surface');
assert.equal(styleValues['--openui-safe-foreground'],undefined,'hostile text must not be copied onto the fallback card surface');
assert.equal(styleValues['--openui-safe-primary'],undefined,'white-on-white button theme must fall back');
const openuiCss=fs.readFileSync(require.resolve('../static/openui.css'),'utf8');
const legacyCss=fs.readFileSync(require.resolve('../static/style.css'),'utf8');
assert.ok(openuiCss.includes('.openui-miniapp'),'OpenUI CSS must be rooted at its container');
assert.equal(openuiCss.includes(':root'),false,'OpenUI CSS must not mutate root variables');
assert.ok(legacyCss.includes('--accent:#1b5cbd'),'legacy theme tokens stay in the legacy stylesheet');
console.log('PASS: hostile theme colors use safe variables; OpenUI CSS is scoped and legacy tokens are isolated.');
