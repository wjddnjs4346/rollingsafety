(() => {
 const media=matchMedia('(prefers-color-scheme: dark)');
 let preference='system';
 try { preference=localStorage.getItem('rolling-theme')||'system'; } catch {}
 if(!['system','light','dark'].includes(preference))preference='system';
 function apply(){document.documentElement.dataset.theme=preference==='system'?(media.matches?'dark':'light'):preference;}
 apply();media.addEventListener('change',apply);
 document.addEventListener('DOMContentLoaded',()=>{const select=document.getElementById('themeSelect');select.value=preference;select.addEventListener('change',()=>{preference=select.value;try{localStorage.setItem('rolling-theme',preference);}catch{}apply();});});
})();
