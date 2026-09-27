const assert=require('assert');
const {scanMap}=require('../companion_map.js');
const w=160,h=90,rgb=Buffer.alloc(w*h*3);
for(let i=0;i<rgb.length;i+=3){rgb[i]=80;rgb[i+1]=140;rgb[i+2]=40;}
assert.equal(scanMap(rgb.toString('base64'),w,h).regions.length,0);
for(let y=35;y<45;y++)for(let x=75;x<85;x++){const i=(y*w+x)*3;rgb[i]=220;rgb[i+1]=35;rgb[i+2]=50;}
const result=scanMap(rgb.toString('base64'),w,h);
assert.equal(result.regions.length,1);
const box=result.regions[0];assert(box[0]<75/w && box[2]>85/w && box[1]<35/h && box[3]>45/h);
assert.throws(()=>scanMap('x',1000,100));assert.throws(()=>scanMap('',w,h));
console.log('PASS: Frida scanner components, terrain rejection, ROI coverage, dimension and payload bounds');
