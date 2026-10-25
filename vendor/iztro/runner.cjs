// Isolated local calculation: request JSON on stdin; no network or disk writes.
'use strict';
global.self=globalThis;
const {astro}=require('./iztro.min.js');
let input='';
process.stdin.setEncoding('utf8');
process.stdin.on('data',chunk=>{input+=chunk;if(input.length>1024)process.exit(2);});
process.stdin.on('end',()=>{
 try{
  const p=JSON.parse(input);
  if(!/^\d{4}-\d{2}-\d{2}$/.test(p.date)||!Number.isInteger(p.timeIndex)||p.timeIndex<0||p.timeIndex>12||!['男','女'].includes(p.gender))process.exit(2);
  // Pin the library's documented defaults rather than depend on global state.
  astro.config({yearDivide:'normal',ageDivide:'normal',dayDivide:'forward',horoscopeDivide:'normal',algorithm:'default'});
  process.stdout.write(JSON.stringify(astro.bySolar(p.date,p.timeIndex,p.gender,true,'zh-CN')));
 }catch(_){process.exitCode=1;}
});
