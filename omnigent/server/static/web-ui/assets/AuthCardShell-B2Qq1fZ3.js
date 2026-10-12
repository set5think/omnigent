import{a as e}from"./rolldown-runtime-CNC7AqOf.js";import{B as t,z as n}from"./streamdown-Bpng-vW9.js";import{s as r}from"./routing-DyFm3e8q.js";import{vt as i}from"./index-CeqxPSKz.js";var a=`omnigent.loginV2`;function o(e){try{e?window.localStorage.setItem(a,`1`):window.localStorage.removeItem(a)}catch{}}function s(){let e=r()[0].get(`login-v2`);if(e===`1`||e===`0`){let t=e===`1`;return o(t),t}try{return window.localStorage.getItem(a)===`1`}catch{return!1}}var c=e(t(),1),l=n(),u=`#version 300 es
in vec2 position;

void main() {
  gl_Position = vec4(position, 0.0, 1.0);
}`,d=`#version 300 es
precision highp float;

uniform vec3 uColor;
uniform vec2 uResolution;
uniform float uTime;
uniform float uPixelSize;
uniform float uScale;
uniform float uDensity;

out vec4 fragColor;

float bayer2(vec2 value) {
  value = floor(value);
  return fract(value.x / 2.0 + value.y * value.y * 0.75);
}

float bayer4(vec2 value) {
  return bayer2(0.5 * value) * 0.25 + bayer2(value);
}

float bayer8(vec2 value) {
  return bayer4(0.5 * value) * 0.25 + bayer2(value);
}

float hash11(float value) {
  return fract(sin(value) * 43758.5453);
}

float valueNoise(vec3 point) {
  vec3 cell = floor(point);
  vec3 local = fract(point);
  vec3 blend = local * local * local * (local * (local * 6.0 - 15.0) + 10.0);
  vec3 stepX = vec3(1.0, 0.0, 0.0);
  vec3 stepY = vec3(0.0, 1.0, 0.0);
  vec3 stepZ = vec3(0.0, 0.0, 1.0);
  vec3 seed = vec3(1.0, 57.0, 113.0);

  float n000 = hash11(dot(cell, seed));
  float n100 = hash11(dot(cell + stepX, seed));
  float n010 = hash11(dot(cell + stepY, seed));
  float n110 = hash11(dot(cell + stepX + stepY, seed));
  float n001 = hash11(dot(cell + stepZ, seed));
  float n101 = hash11(dot(cell + stepX + stepZ, seed));
  float n011 = hash11(dot(cell + stepY + stepZ, seed));
  float n111 = hash11(dot(cell + stepX + stepY + stepZ, seed));

  float x00 = mix(n000, n100, blend.x);
  float x10 = mix(n010, n110, blend.x);
  float x01 = mix(n001, n101, blend.x);
  float x11 = mix(n011, n111, blend.x);
  float y0 = mix(x00, x10, blend.y);
  float y1 = mix(x01, x11, blend.y);
  return mix(y0, y1, blend.z) * 2.0 - 1.0;
}

float fbm(vec2 uv, float time) {
  vec3 point = vec3(uv * uScale, time);
  float frequency = 1.0;
  float total = 1.0;

  for (int octave = 0; octave < 5; octave++) {
    total += valueNoise(point * frequency);
    frequency *= 1.25;
  }

  return total * 0.5 + 0.5;
}

void main() {
  vec2 centered = gl_FragCoord.xy - uResolution * 0.5;
  float aspect = uResolution.x / uResolution.y;
  vec2 pixelId = floor(centered / uPixelSize);
  float cellSize = 8.0 * uPixelSize;
  vec2 cell = floor(centered / cellSize) * cellSize;
  vec2 uv = cell / uResolution * vec2(aspect, 1.0);

  float base = fbm(uv, uTime * 0.05);
  float feed = (base * 0.5 - 0.65) + (uDensity - 0.5) * 0.3;
  float dither = bayer8(centered / uPixelSize) - 0.5;
  float coverage = step(0.5, feed + dither);

  vec3 srgb = mix(
    uColor * 12.92,
    1.055 * pow(uColor, vec3(1.0 / 2.4)) - 0.055,
    step(vec3(0.0031308), uColor)
  );

  fragColor = vec4(srgb, coverage);
}`;function f(e,t,n){let r=e.createShader(t);if(!r)throw Error(`Unable to create WebGL shader.`);if(e.shaderSource(r,n),e.compileShader(r),!e.getShaderParameter(r,e.COMPILE_STATUS)){let t=e.getShaderInfoLog(r)||`Unknown shader compilation error.`;throw e.deleteShader(r),Error(t)}return r}function p(e){let t=f(e,e.VERTEX_SHADER,u),n=f(e,e.FRAGMENT_SHADER,d),r=e.createProgram();if(!r)throw Error(`Unable to create WebGL program.`);if(e.attachShader(r,t),e.attachShader(r,n),e.linkProgram(r),e.deleteShader(t),e.deleteShader(n),!e.getProgramParameter(r,e.LINK_STATUS)){let t=e.getProgramInfoLog(r)||`Unknown program link error.`;throw e.deleteProgram(r),Error(t)}return r}function m(e){let t=e.replace(`#`,``),n=t.length===3?t.split(``).map(e=>e+e).join(``):t,r=Number.parseInt(n,16),i=e=>{let t=e/255;return t<=.04045?t/12.92:((t+.055)/1.055)**2.4};return[i(r>>16&255),i(r>>8&255),i(r&255)]}function h({pixelSize:e=1.5,color:t=`#f9a8d4`,patternScale:n=3.5,patternDensity:r=1.3,speed:i=.5,className:a=``,style:o}){let s=(0,c.useRef)(null);return(0,c.useEffect)(()=>{let a=s.current;if(!a)return;let o=a.getContext(`webgl2`,{alpha:!0,antialias:!1,premultipliedAlpha:!0,powerPreference:`high-performance`});if(!o){a.dataset.webglUnavailable=`true`;return}if(o.isContextLost())return;let c;try{c=p(o)}catch(e){o.isContextLost()||(console.error(`PixelBlast WebGL2 initialization failed:`,e),a.dataset.webglUnavailable=`true`);return}let l=o.createVertexArray(),u=o.createBuffer();if(!l||!u){o.deleteProgram(c);return}o.bindVertexArray(l),o.bindBuffer(o.ARRAY_BUFFER,u),o.bufferData(o.ARRAY_BUFFER,new Float32Array([-1,-1,3,-1,-1,3]),o.STATIC_DRAW);let d=o.getAttribLocation(c,`position`);o.enableVertexAttribArray(d),o.vertexAttribPointer(d,2,o.FLOAT,!1,0,0),o.useProgram(c);let f=o.getUniformLocation(c,`uResolution`),h=o.getUniformLocation(c,`uTime`),g=o.getUniformLocation(c,`uColor`),_=o.getUniformLocation(c,`uPixelSize`),v=o.getUniformLocation(c,`uScale`),y=o.getUniformLocation(c,`uDensity`),[b,x,S]=m(t);o.uniform3f(g,b,x,S),o.uniform1f(v,n),o.uniform1f(y,r),o.clearColor(0,0,0,0),o.enable(o.BLEND),o.blendFunc(o.SRC_ALPHA,o.ONE_MINUS_SRC_ALPHA);let C=1,w=()=>{let t=a.getBoundingClientRect();C=Math.min(window.devicePixelRatio||1,1),a.width=Math.max(1,Math.round(t.width*C)),a.height=Math.max(1,Math.round(t.height*C)),o.viewport(0,0,a.width,a.height),o.useProgram(c),o.uniform2f(f,a.width,a.height),o.uniform1f(_,e*C)},T=new ResizeObserver(w);T.observe(a),w();let E=performance.now(),D=Math.random()*1e3,O=0,k=window.matchMedia(`(prefers-reduced-motion: reduce)`),A=!0,j=()=>i>0&&A&&!document.hidden&&!k.matches,M=-1/0,N=e=>{o.clear(o.COLOR_BUFFER_BIT),o.useProgram(c),o.bindVertexArray(l),o.uniform1f(h,D+(e-E)/1e3*i),o.drawArrays(o.TRIANGLES,0,3),M=e},P=e=>{e-M>=33.333333333333336&&N(e),j()&&(O=requestAnimationFrame(P))},F=()=>{O||!j()||(O=requestAnimationFrame(P))},I=()=>{cancelAnimationFrame(O),O=0},L=()=>j()?F():(I(),N(performance.now())),R=new IntersectionObserver(([e])=>{A=e.isIntersecting,L()});return R.observe(a),document.addEventListener(`visibilitychange`,L),k.addEventListener(`change`,L),P(performance.now()),()=>{I(),R.disconnect(),document.removeEventListener(`visibilitychange`,L),k.removeEventListener(`change`,L),T.disconnect(),o.deleteBuffer(u),o.deleteVertexArray(l)}},[t,r,n,e,i]),(0,l.jsx)(`canvas`,{ref:s,className:`pixel-blast-canvas ${a}`,style:o,"aria-hidden":`true`})}var g=``+new URL(`omnigent-starfish-icon-DPpioQBB.png`,import.meta.url).href,_=440,v=56;function y({height:e=560,panelHeight:t=308,autoHeight:n=!1,centeredLogo:r=!1,children:i}){let a=typeof window<`u`&&window.matchMedia(`(prefers-reduced-motion: reduce)`).matches;return(0,l.jsxs)(`section`,{className:`omnigent-card${n?` omnigent-card--auto`:``}`,style:{width:_,...n?{}:{height:e}},"aria-label":`Omnigent onboarding`,children:[(0,l.jsxs)(`div`,{className:`omnigent-animated-panel`,style:{height:t},children:[(0,l.jsx)(`div`,{className:`omnigent-pixel-field`,"aria-hidden":`true`,children:(0,l.jsx)(h,{speed:a?0:.5})}),(0,l.jsx)(`img`,{src:g,alt:`Omnigent`,className:`omnigent-panel-logo${r?` omnigent-panel-logo--centered`:``}`,style:{width:v,height:v}})]}),i!=null&&(0,l.jsx)(`div`,{className:`omnigent-card-body`,children:i})]})}var b=220,x=200;function S({panelHeight:e=b,children:t}){return i()?(0,l.jsx)(`div`,{className:`flex min-h-screen flex-col items-center bg-background px-4`,style:{paddingTop:`calc(var(--omnigent-safe-top) + 3rem)`,paddingBottom:`calc(var(--omnigent-safe-bottom) + 3rem)`},children:(0,l.jsxs)(`div`,{className:`flex w-full max-w-sm flex-1 flex-col justify-center gap-6`,children:[(0,l.jsx)(`img`,{src:g,alt:`Omnigent`,className:`mx-auto size-14 object-contain`,"aria-hidden":`true`}),t]})}):(0,l.jsx)(`div`,{className:`flex min-h-screen justify-center overflow-y-auto bg-background p-6 [&>*]:my-auto`,style:{paddingTop:`var(--omnigent-safe-top)`,paddingBottom:`var(--omnigent-safe-bottom)`},children:(0,l.jsx)(y,{panelHeight:Math.max(e,x),autoHeight:!0,centeredLogo:!0,children:(0,l.jsx)(`div`,{className:`flex flex-col px-2 pb-2 pt-3`,children:t})})})}export{s as n,S as t};