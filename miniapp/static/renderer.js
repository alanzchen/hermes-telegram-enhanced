/* Fixed catalog: never parse model markup, assign innerHTML, or evaluate code. */
(function(root,factory){const api=factory(typeof module==='object'&&module.exports?require('./layout.js'):root.HteLayout);if(typeof module==='object'&&module.exports)module.exports=api;else root.HteRenderer=api;})(globalThis,function(L){
'use strict';
const colors=['#2875d9','#008778','#a05bd4','#cc6b14','#cb4868','#527135'];
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);if(cls)n.className=cls;return n;}
function svg(tag,attrs={},text){const n=document.createElementNS('http://www.w3.org/2000/svg',tag);Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,String(v)));if(text!==undefined)n.textContent=String(text);return n;}
function rich(n,text,markdown){
 if(!markdown){n.textContent=text;return;}
 const parts=text.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`|\*[^*\n]+\*)/g);
 parts.forEach(s=>{let tag=null,content=s;if(s.startsWith('**')&&s.endsWith('**')){tag='strong';content=s.slice(2,-2);}else if(s.startsWith('`')&&s.endsWith('`')){tag='code';content=s.slice(1,-1);}else if(s.startsWith('*')&&s.endsWith('*')){tag='em';content=s.slice(1,-1);}n.append(tag?el(tag,content):document.createTextNode(content));});
}
function drawing(width,height,title){const n=svg('svg',{viewBox:`0 0 ${width} ${height}`,role:'img','aria-label':title||'Diagram',class:'drawing'});n.append(svg('title',{},title||'Diagram'));return n;}
function wrapDrawing(n,wide=false){const wrap=el('div',undefined,wide?'diagram-scroll':'chart-wrap');if(wide){wrap.tabIndex=0;wrap.setAttribute('aria-label','Scrollable diagram');}wrap.append(n);return wrap;}
function short(s,n=18){return s.length>n?s.slice(0,n-1)+'…':s;}
function svgLabel(n,label,x,y,max=20){const chars=Array.from(label),lines=[];for(let i=0;i<chars.length;i+=max)lines.push(chars.slice(i,i+max).join(''));const text=svg('text',{x,y:y-(Math.min(3,lines.length)-1)*8,'text-anchor':'middle',class:'node-text'});lines.slice(0,3).forEach((line,i)=>text.append(svg('tspan',{x,dy:i?16:0},i===2&&lines.length>3?short(line,18):line)));n.append(text);}
function table(c){const wrap=el('div',undefined,'table-scroll'),t=el('table'),head=el('thead'),tr=el('tr'),body=el('tbody');let rows=c.rows.slice(),column=-1,descending=false;
 function paint(){body.replaceChildren();rows.forEach(row=>{const r=el('tr');row.forEach(cell=>r.append(el('td',cell===null?'—':cell)));body.append(r);});}
 c.columns.forEach((name,i)=>{const th=el('th');if(c.sortable){const b=el('button',name,'sort-button');b.type='button';b.addEventListener('click',()=>{descending=column===i?!descending:false;column=i;rows.sort((a,b)=>{const x=a[i],y=b[i];return (typeof x==='number'&&typeof y==='number'?x-y:String(x??'').localeCompare(String(y??''),undefined,{numeric:true}))*(descending?-1:1);});Array.from(tr.children).forEach(x=>x.setAttribute('aria-sort','none'));th.setAttribute('aria-sort',descending?'descending':'ascending');paint();});th.append(b);}else th.textContent=name;tr.append(th);});head.append(tr);t.append(head,body);paint();wrap.append(t);return wrap;}
function chart(c){
 const card=el('section',undefined,'card');if(c.title)card.append(el('h2',c.title));const width=600,height=c.kind==='hbar'?Math.max(200,c.labels.length*44+70):290,n=drawing(width,height,c.title||'Chart');
 const left=c.kind==='hbar'?130:56,right=24,top=26,bottom=50,pw=width-left-right,ph=height-top-bottom,all=c.series.flatMap(s=>s.values),lo=Math.min(0,...all),hi=Math.max(0,...all),range=hi-lo||1,xval=v=>left+(v-lo)/range*pw,yval=v=>top+ph-(v-lo)/range*ph;
 if(c.kind==='pie-lite'){
   const total=all.reduce((a,b)=>a+b,0);let angle=-Math.PI/2;
   c.labels.forEach((label,i)=>{const value=all[i],end=angle+value/total*Math.PI*2,cx=300,cy=140,r=106;let shape;
    if(value===total)shape=svg('circle',{cx,cy,r,fill:colors[i%colors.length]});
    else shape=svg('path',{d:`M ${cx} ${cy} L ${cx+Math.cos(angle)*r} ${cy+Math.sin(angle)*r} A ${r} ${r} 0 ${end-angle>Math.PI?1:0} 1 ${cx+Math.cos(end)*r} ${cy+Math.sin(end)*r} Z`,fill:colors[i%colors.length]});
    shape.append(svg('title',{},`${label}: ${value}`));n.append(shape);angle=end;
   });
 }else{
  for(let tick=0;tick<5;tick++){const v=lo+range*tick/4,shown=Number(v.toPrecision(4));if(c.kind==='hbar'){const x=xval(v);n.append(svg('line',{x1:x,x2:x,y1:top,y2:height-bottom,class:'grid-line'}),svg('text',{x,y:height-25,'text-anchor':'middle',class:'axis-text'},shown));}else{const y=yval(v);n.append(svg('line',{x1:left,x2:width-right,y1:y,y2:y,class:'grid-line'}),svg('text',{x:left-8,y:y+4,'text-anchor':'end',class:'axis-text'},shown));}}
  c.series.forEach((s,j)=>{let points=[];const color=colors[j%colors.length];c.labels.forEach((label,i)=>{const v=s.values[i];let mark;
   if(c.kind==='hbar'){const band=ph/c.labels.length,y=top+i*band+j*band*.7/c.series.length;mark=svg('rect',{x:Math.min(xval(0),xval(v)),y,width:Math.abs(xval(v)-xval(0)),height:band*.65/c.series.length,rx:3,fill:color});if(!j)n.append(svg('text',{x:left-10,y:y+14,'text-anchor':'end',class:'axis-text'},short(label,16)));}
   else if(c.kind==='bar'){const band=pw/c.labels.length,bw=band*.72/c.series.length,x=left+i*band+band*.14+j*bw;mark=svg('rect',{x,y:Math.min(yval(0),yval(v)),width:Math.max(1,bw-2),height:Math.abs(yval(0)-yval(v)),rx:3,fill:color});}
   else{const x=left+(c.labels.length===1?pw/2:i*pw/(c.labels.length-1)),y=yval(v);points.push(`${x},${y}`);mark=svg('circle',{cx:x,cy:y,r:3.5,fill:color});}
   mark.append(svg('title',{},`${s.name} · ${label}: ${v}${c.unit?' '+c.unit:''}`));n.append(mark);
  });if(c.kind==='line')n.append(svg('polyline',{points:points.join(' '),fill:'none',stroke:color,'stroke-width':2.5}));});
  if(c.kind!=='hbar')c.labels.forEach((label,i)=>{if(i%Math.max(1,Math.ceil(c.labels.length/8)))return;const x=c.kind==='bar'?left+(i+.5)*pw/c.labels.length:left+(c.labels.length===1?pw/2:i*pw/(c.labels.length-1));n.append(svg('text',{x,y:height-22,'text-anchor':'middle',class:'axis-text'},short(label,10)));});
 }
 card.append(wrapDrawing(n));const legend=el('div',undefined,'legend');(c.kind==='pie-lite'?c.labels:c.series.map(s=>s.name)).forEach((name,i)=>{const chip=el('span',name);chip.prepend?.(el('span','●','swatch swatch-'+i%6));legend.append(chip);});card.append(legend);
 const details=el('details'),summary=el('summary','View data');details.append(summary,table({columns:['Label',...c.series.map(s=>s.name)],rows:c.labels.map((v,i)=>[v,...c.series.map(s=>s.values[i])])}));card.append(details);if(c.source)card.append(el('p',c.source,'muted'));return card;
}
function gantt(c){const g=L.ganttLayout(c.tasks,c.today),n=drawing(g.width,g.height,c.title||'Schedule');
 g.ticks.forEach(t=>n.append(svg('line',{x1:t.x,x2:t.x,y1:28,y2:g.height,class:'grid-line'}),svg('text',{x:t.x,y:18,'text-anchor':'middle',class:'axis-text'},t.label)));
 g.groups.forEach(group=>n.append(svg('text',{x:8,y:group.y+15,class:'group-text'},group.label)));
 const byid=new Map(g.tasks.map(t=>[t.id,t]));g.tasks.forEach(t=>(t.depends_on||[]).forEach(id=>{const from=byid.get(id);if(!from)return;const x=from.x+from.w,y=from.y+from.h/2,end=t.x,ey=t.y+t.h/2;n.append(svg('path',{d:`M${x},${y} H${x+9} V${ey} H${end}`,class:'edge',fill:'none'}),svg('path',{d:`M${end-5},${ey-4} L${end},${ey} L${end-5},${ey+4}`,class:'edge',fill:'none'}));}));
 g.tasks.forEach(t=>{n.append(svg('text',{x:8,y:t.y+15,class:'axis-text'},short(t.label,20)));let mark=t.milestone?svg('polygon',{points:`${t.x},${t.y+11} ${t.x+9},${t.y+2} ${t.x+18},${t.y+11} ${t.x+9},${t.y+20}`,class:'task-bar'}):svg('rect',{x:t.x,y:t.y,width:t.w,height:t.h,rx:5,class:'task-bar'});mark.append(svg('title',{},`${t.label}: ${t.start} – ${t.end}`));n.append(mark);if(t.progress&&!t.milestone)n.append(svg('rect',{x:t.x,y:t.y,width:t.w*t.progress,height:t.h,rx:5,class:'task-progress'}));});
 if(g.todayX!==null)n.append(svg('line',{x1:g.todayX,x2:g.todayX,y1:28,y2:g.height,class:'today-line'}),svg('text',{x:g.todayX+4,y:35,class:'axis-text'},'Today'));
 const card=el('section',undefined,'card');card.append(el('h2',c.title||'Schedule'),el('p','Scroll sideways for the full schedule.','muted'),wrapDrawing(n,true));return card;
}
function flow(c){const g=L.flowLayout(c.nodes,c.edges),n=drawing(g.width,g.height,c.title||'Flow'),byid=new Map(g.nodes.map(n=>[n.id,n]));
 g.edges.forEach(e=>{const a=byid.get(e.from),b=byid.get(e.to),sx=a.x+a.w/2,sy=a.y+a.h,tx=b.x+b.w/2,ty=b.y,mid=(sy+ty)/2;let d=`M${sx},${sy} V${mid} H${tx} V${ty}`;if(e.back)d=`M${a.x+a.w},${a.y+a.h/2} H${g.width-10} V${b.y-15} H${tx} V${ty}`;n.append(svg('path',{d,fill:'none',class:e.back?'edge back-edge':'edge'}),svg('path',{d:`M${tx-4},${ty-7} L${tx},${ty} L${tx+4},${ty-7}`,fill:'none',class:'edge'}));if(e.label)n.append(svg('text',{x:(sx+tx)/2+5,y:mid-7,class:'edge-label'},short(e.label,16)));});
 g.nodes.forEach(node=>{const {x,y,w,h}=node;const shape=node.shape==='diamond'?svg('polygon',{points:`${x+w/2},${y} ${x+w},${y+h/2} ${x+w/2},${y+h} ${x},${y+h/2}`,class:'flow-node'}):svg('rect',{x,y,width:w,height:h,rx:12,class:'flow-node'});shape.append(svg('title',{},node.label));n.append(shape);svgLabel(n,node.label,x+w/2,y+h/2+5,node.shape==='diamond'?13:20);});
 const card=el('section',undefined,'card');card.append(el('h2',c.title||'Flow'));if(g.width>600)card.append(el('p','Scroll sideways for the full workflow.','muted'));card.append(wrapDrawing(n,g.width>600));return card;
}
function render(root,record,onAction,uiState={}){
 const results=record.results||{};const disabled=record.closed||Date.now()/1000>=record.expires_at;
 function status(a,c){const r=results[a.key];if(r&&['pending','uncertain'].includes(r.status))return r;if(r?.status==='recorded'&&(a.once!==false||r.once))return r;if(c.type==='buttons')return Object.values(results).find(x=>x.component_id===c.id&&x.status==='recorded'&&(c.once!==false||x.group_once));return null;}
 function submitButton(a,c,value,parent,controls=[]){const b=el('button',a.label,'primary');b.type='button';b.dataset.action=a.key;const used=status(a,c);b.disabled=!!disabled||!!used;controls.forEach(x=>x.disabled=b.disabled);if(used){const label=used.status==='recorded'?'Recorded':'Delivery pending / uncertain';const note=el('p',label+(used.selection_labels?' · '+used.selection_labels.join(', '):''),'recorded');note.setAttribute('role','status');parent.append(note);}
 b.addEventListener('click',async()=>{if(b.disabled)return;let v;try{v=value();}catch(e){const note=el('p',e.message,'error');note.setAttribute('role','alert');parent.append(note);return;}b.disabled=true;try{await onAction(a.key,v);}finally{if(!status(a,c)&&!disabled)b.disabled=false;}});parent.append(b);return b;}
 function component(c){let n;
 switch(c.type){
 case 'heading':return el('h'+(c.level||2),c.text);
 case 'text':n=el('p',undefined,'prose');rich(n,c.text,c.markdown);return n;
 case 'callout':n=el('aside',undefined,'callout '+(c.tone||'info'));if(c.title)n.append(el('strong',c.title));n.append(el('p',c.text));return n;
 case 'divider':return el('hr');
 case 'kv':n=el('dl',undefined,'kv card');c.items.forEach(i=>n.append(el('dt',i.key),el('dd',i.value)));return n;
 case 'table':return table(c);
 case 'list':n=el(c.ordered?'ol':'ul');c.items.forEach(i=>n.append(el('li',i)));return n;
 case 'checklist':n=el('ul',undefined,'checklist');c.items.forEach(i=>n.append(el('li',(i.checked?'☑ ':'☐ ')+i.text)));return n;
 case 'chart':return chart(c);
 case 'gantt':return gantt(c);
 case 'flow':return flow(c);
 case 'timeline':n=el('ol',undefined,'timeline');c.items.forEach(i=>{const item=el('li');if(i.date)item.append(el('small',i.date,'muted'));item.append(el('h3',i.title));if(i.text)item.append(el('p',i.text));n.append(item);});return n;
 case 'image':n=el('figure');{const img=el('img');img.src=c.url;img.alt=c.alt||'';img.loading='lazy';img.referrerPolicy='no-referrer';n.append(img);if(c.caption)n.append(el('figcaption',c.caption));}return n;
 case 'section':n=el('section',undefined,'section');n.append(el('h2',c.title));c.components.forEach(x=>n.append(component(x)));return n;
 case 'tabs':{
   n=el('section',undefined,'tabs');const bar=el('div',undefined,'tab-bar');bar.setAttribute('role','tablist');const panes=[],buttons=[];const stateKey='tabs:'+c.tabs.map(t=>t.label).join('|');let selected=uiState[stateKey]||0;
   function select(index){selected=index;uiState[stateKey]=index;panes.forEach((p,j)=>p.hidden=j!==index);buttons.forEach((b,j)=>{b.setAttribute('aria-selected',String(j===index));b.tabIndex=j===index?0:-1;});}
   c.tabs.forEach((tab,i)=>{const b=el('button',tab.label,'tab-button'),p=el('div',undefined,'tab-panel');b.type='button';b.setAttribute('role','tab');p.setAttribute('role','tabpanel');b.addEventListener('click',()=>select(i));b.addEventListener('keydown',e=>{let next=null;if(e.key==='ArrowRight')next=(i+1)%c.tabs.length;if(e.key==='ArrowLeft')next=(i-1+c.tabs.length)%c.tabs.length;if(next!==null){e.preventDefault();select(next);buttons[next].focus();}});tab.components.forEach(x=>p.append(component(x)));buttons.push(b);panes.push(p);bar.append(b);});n.append(bar,...panes);select(Math.min(selected,panes.length-1));return n;
 }
 case 'choice':{
   n=el('section',undefined,'card');n.append(el('h2',c.label));const controls=[],prior=results[c.action.key],draft=uiState[c.id]??prior?.value??(c.multiple?[]:'');const selected=new Set(Array.isArray(draft)?draft:[draft]);
   c.options.forEach(o=>{const label=el('label',undefined,'option'),input=el('input');input.type=c.multiple?'checkbox':'radio';input.name=c.id;input.value=o.value;input.checked=selected.has(o.value);input.addEventListener('change',()=>{uiState[c.id]=c.multiple?controls.filter(x=>x.checked).map(x=>x.value):input.value;});const detail=el('span',undefined,'option-copy');detail.append(el('strong',o.label));if(o.details)detail.append(el('span',o.details,'option-details'));label.append(input,detail);controls.push(input);n.append(label);});
   submitButton(c.action,c,()=>{const values=controls.filter(x=>x.checked).map(x=>x.value);if(!values.length)throw Error('Choose an option first.');return c.multiple?values:values[0];},n,controls);return n;
 }
 case 'form':{
   n=el('section',undefined,'card');if(c.title)n.append(el('h2',c.title));n.append(el('p','Values are sent to the assistant. Do not enter passwords or authentication codes.','privacy-note'));
   const fields={},draft=uiState[c.id]??results[c.action.key]?.value??{};
   c.fields.forEach(f=>{const label=el('label',undefined,'field');label.append(el('span',f.label+(f.required?' *':'')));const input=el(f.type==='select'?'select':'input');input.name=f.key;
    if(f.type==='select'){if(!f.required){const opt=el('option','Select…');opt.value='';input.append(opt);}f.options.forEach(o=>{const opt=el('option',o.label);opt.value=o.value;input.append(opt);});}
    else input.type=f.type==='toggle'?'checkbox':f.type;
    const value=Object.hasOwn(draft,f.key)?draft[f.key]:f.default;
    if(f.type==='toggle')input.checked=value===true;else if(value!==undefined&&value!==null)input.value=String(value);
    input.required=!!f.required;if(f.type==='number'){input.step='any';if(f.min!==undefined)input.min=f.min;if(f.max!==undefined)input.max=f.max;}if(f.type==='text')input.maxLength=f.max_length||1000;if(f.placeholder)input.placeholder=f.placeholder;
    input.addEventListener('input',()=>{const next={...(uiState[c.id]||draft)};next[f.key]=f.type==='toggle'?input.checked:input.value;uiState[c.id]=next;});fields[f.key]=input;label.append(input);n.append(label);});
   submitButton(c.action,c,()=>{const values={};for(const f of c.fields){const input=fields[f.key];if(f.type==='toggle'){values[f.key]=input.checked;continue;}if(!input.checkValidity()){input.reportValidity();throw Error('Check '+f.label+'.');}if(!input.value&&!f.required)continue;values[f.key]=f.type==='number'?Number(input.value):input.value;}return values;},n,Object.values(fields));return n;
 }
 case 'buttons':n=el('div',undefined,'actions');c.actions.forEach(a=>submitButton(a,c,()=>null,n));return n;
 default:return el('p','Unsupported component','error');
 }
 }
 const fragment=el('div',undefined,'components');record.spec.components.forEach(c=>fragment.append(component(c)));root.replaceChildren(fragment);
}
return {render};
});
