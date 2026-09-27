// Runs inside our isolated companion's Frida session, never inside RoK.
// Input is a downscaled RGB screenshot supplied by ADB on the host.
function scanMap(encoded, width, height) {
  if (!Number.isInteger(width) || !Number.isInteger(height) || width < 8 || height < 8 || width > 320 || height > 320)
    throw new Error('Invalid image dimensions');
  if (encoded.length > 410000) throw new Error('Image too large');
  const alphabet='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  const rgb=new Uint8Array(width*height*3);let acc=0,bits=0,pos=0;
  for (const ch of encoded) {
    if(ch==='=')break;
    const value=alphabet.indexOf(ch);if(value<0)throw new Error('Invalid base64');
    acc=(acc<<6)|value;bits+=6;
    if(bits>=8){bits-=8;if(pos>=rgb.length)throw new Error('Excess pixels');rgb[pos++]=(acc>>bits)&255;}
  }
  if(pos!==rgb.length)throw new Error('Truncated pixels');
  const mask=new Uint8Array(width*height),seen=new Uint8Array(width*height),boxes=[];
  for(let y=Math.ceil(height*.06);y<height*.91;y++)for(let x=Math.ceil(width*.035);x<width*.965;x++){
    const i=y*width+x,r=rgb[i*3],g=rgb[i*3+1],b=rgb[i*3+2];
    mask[i]=r>65 && r>g*1.23 && r>b*1.12 && r-Math.min(g,b)>35 ? 1:0;
  }
  for(let i=0;i<mask.length;i++){
    if(!mask[i]||seen[i])continue;
    const queue=[i];seen[i]=1;let count=0,minx=width,miny=height,maxx=0,maxy=0;
    for(let q=0;q<queue.length;q++){
      const index=queue[q],x=index%width,y=Math.floor(index/width);count++;
      minx=Math.min(minx,x);maxx=Math.max(maxx,x);miny=Math.min(miny,y);maxy=Math.max(maxy,y);
      for(let dy=-1;dy<=1;dy++)for(let dx=-1;dx<=1;dx++){
        const nx=x+dx,ny=y+dy;if(nx<0||ny<0||nx>=width||ny>=height)continue;
        const next=ny*width+nx;if(mask[next]&&!seen[next]){seen[next]=1;queue.push(next);}
      }
    }
    if(count<4)continue;
    const w=maxx-minx+1,h=maxy-miny+1;
    if(w/h<.18||w/h>6||w*h>width*height*.15)continue;
    const padx=Math.max(9,w*.9),pady=Math.max(9,h*.8);
    boxes.push({red_pixels:count,region:[Math.max(.035,(minx-padx)/width),Math.max(.055,(miny-pady)/height),Math.min(.965,(maxx+padx+1)/width),Math.min(.915,(maxy+pady+1)/height)]});
  }
  boxes.sort((a,b)=>b.red_pixels-a.red_pixels);
  return {regions:boxes.slice(0,12).map(x=>x.region),components:boxes.length,engine:'frida-companion-rgb'};
}
if (typeof rpc !== 'undefined') rpc.exports = {scanmap:scanMap};
if (typeof module !== 'undefined') module.exports = {scanMap};
