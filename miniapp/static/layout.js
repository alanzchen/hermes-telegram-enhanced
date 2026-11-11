/* Deterministic geometry only; no DOM. Cycles use a stable DFS back-edge cut. */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.HteLayout=api;})(globalThis,function(){
'use strict';
function flowLayout(nodes,edges){
 const index=new Map(nodes.map((n,i)=>[n.id,i])),next=new Map(nodes.map(n=>[n.id,[]])),state=new Map(),back=new Set(),order=[];
 edges.forEach((e,i)=>{if(index.has(e.from)&&index.has(e.to))next.get(e.from).push([e.to,i]);});
 function visit(id){state.set(id,1);for(const [to,i] of next.get(id)){if(state.get(to)===1)back.add(i);else if(!state.has(to))visit(to);}state.set(id,2);order.push(id);}
 nodes.forEach(n=>{if(!state.has(n.id))visit(n.id);});order.reverse();
 const rank=new Map(nodes.map(n=>[n.id,0]));
 for(const id of order)for(const [to,i] of next.get(id))if(!back.has(i))rank.set(to,Math.max(rank.get(to),rank.get(id)+1));
 const layers=[];nodes.forEach(n=>{const r=rank.get(n.id);(layers[r]||= []).push(n);});
 // Barycenter ordering reduces simple branch crossings. Stable source order breaks ties.
 for(let pass=0;pass<2;pass++)for(let r=1;r<layers.length;r++){
   const previous=new Map(layers[r-1].map((n,i)=>[n.id,i]));
   const score=n=>{const ps=edges.filter(e=>e.to===n.id&&previous.has(e.from)).map(e=>previous.get(e.from));return ps.length?ps.reduce((a,b)=>a+b,0)/ps.length:index.get(n.id);};
   layers[r].sort((a,b)=>score(a)-score(b)||index.get(a.id)-index.get(b.id));
 }
 const w=152,h=76,gapX=32,gapY=64,margin=28,columns=Math.max(...layers.map(l=>l.length)),width=columns*(w+gapX)-gapX+margin*2;
 const result=layers.flatMap((layer,r)=>layer.map((n,i)=>({...n,x:(width-(layer.length*w+(layer.length-1)*gapX))/2+i*(w+gapX),y:margin+r*(h+gapY),w,h})));
 return {nodes:result,edges:edges.map((e,i)=>({...e,back:back.has(i)})),width,height:layers.length*(h+gapY)-gapY+margin*2};
}
function ganttLayout(tasks,today){
 const day=s=>Date.parse(s+'T00:00:00Z')/86400000,min=Math.min(...tasks.map(t=>day(t.start))),max=Math.max(...tasks.map(t=>day(t.end))),span=Math.max(1,max-min+1);
 const left=148,right=24,plot=Math.max(350,Math.min(900,span*26)),width=left+plot+right;let y=50,group=null;const groups=[];
 const result=tasks.map(t=>{if(t.group&&t.group!==group){group=t.group;groups.push({label:group,y});y+=30;}const row={...t,x:left+(day(t.start)-min)/span*plot,y,w:Math.max(8,(day(t.end)-day(t.start)+1)/span*plot),h:22};y+=40;return row;});
 const ticks=Array.from({length:5},(_,i)=>{const d=min+Math.round((span-1)*i/4);return {x:left+(d-min)/span*plot,label:new Date(d*86400000).toISOString().slice(5,10)};}).filter((v,i,a)=>!i||v.label!==a[i-1].label);
 const current=day(today||new Date().toISOString().slice(0,10));
 return {tasks:result,groups,ticks,width,height:y+12,left,todayX:current>=min&&current<=max?left+(current-min)/span*plot:null};
}
return {flowLayout,ganttLayout};
});
