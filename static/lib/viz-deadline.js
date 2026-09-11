// A message-delivery deadline is not a measurement of source execution time.
// The worker reports execution separately; this fuse only terminates a stuck
// worker or broken transport. Hidden documents may be suspended by the browser.
export class FrameDeadline {
  constructor(expired,{deliveryMs=3000,setTimer=setTimeout,clearTimer=clearTimeout,isHidden=()=>document.hidden}={}){
    this.expired=expired;this.deliveryMs=deliveryMs;this.setTimer=(...args)=>setTimer(...args);this.clearTimer=(...args)=>clearTimer(...args);this.isHidden=isHidden;this.timer=null;this.request=null;
  }
  arm(request){this.clear();this.request=request;this.schedule();}
  schedule(){
    if(this.request===null||this.isHidden())return;
    this.timer=this.setTimer(()=>{
      this.timer=null;
      if(this.isHidden())return;
      const request=this.request;this.request=null;this.expired(request);
    },this.deliveryMs);
  }
  visibilityChanged(){
    if(this.timer!==null){this.clearTimer(this.timer);this.timer=null;}
    if(this.request!==null)this.schedule();
  }
  clear(){if(this.timer!==null)this.clearTimer(this.timer);this.timer=null;this.request=null;}
}
