// Vendored Simple Icons 16.11.0 marks; no remote fetch in a frame. Each node
// gets a cached icon under one monochrome, 18px style contract.
const marks = {
  'service:github':'github', 'service:github-actions':'github',
  'service:whatsapp':'whatsapp', 'toolchain:npm':'npm',
  'script:python3':'python', 'container':'docker', 'database':'sqlite',
};
export class IconCache {
  constructor(){ this.nodes=new Map(); this.images=new Map(); }
  get(id){
    if(this.nodes.has(id)) return this.nodes.get(id);
    const name=marks[id] || (id.startsWith('repo:') ? 'git' : null);
    let entry=null;
    if(name){
      entry=this.images.get(name);
      if(!entry){
        const image=new Image(); entry={image,ready:false};
        image.onload=()=>{entry.ready=true;};
        image.src=`/static/img/viz/${name}.svg`;
        this.images.set(name,entry);
      }
    }
    this.nodes.set(id,entry);
    return entry;
  }
}
