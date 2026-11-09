"""911 RSS-only continuous episode; read-only state, no recovery effects."""
import math

class RSSContinuity:
    def __init__(self, baseline):
        value=baseline.get('rss_median_bytes')
        if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
            raise ValueError('RSS_baseline_unknown')
        self.limit=1.5*value
        self.identity=None;self.first=None;self.last=None;self.last_wall=None

    def reset(self):
        self.identity=None;self.first=None;self.last=None;self.last_wall=None

    def sample(self,row,stamp,boot_window):
        value=row.get('rss_bytes')
        identity=(row.get('pid'),row.get('process_start'),row.get('boot'))
        wall=row.get('epoch')
        valid=(type(wall) in (int,float) and math.isfinite(wall) and type(value) is int and value>0 and type(stamp) in (int,float)
            and math.isfinite(stamp) and type(identity[0]) is int and identity[0]>0
            and type(identity[1]) is int and identity[1]>0
            and isinstance(identity[2],str) and bool(identity[2])
            and row.get('rss_pid')==identity[0] and row.get('rss_start')==identity[1])
        high=valid and value>self.limit
        if boot_window is not False:
            self.reset()
            return {'trigger':False,'state':'SUPPRESSED_830_BOOT' if boot_window is True else 'UNKNOWN_BOOT_BINDING',
                    'high_sample':bool(high),'limit_bytes':self.limit,'continuous_seconds':0}
        if not valid:
            self.reset()
            return {'trigger':False,'state':'UNKNOWN','high_sample':False,
                    'limit_bytes':self.limit,'continuous_seconds':0}
        if not high:
            self.reset()
            return {'trigger':False,'state':'NOT_ABOVE','high_sample':False,
                    'limit_bytes':self.limit,'continuous_seconds':0}
        if (identity!=self.identity or self.last is None or stamp<=self.last
                or stamp-self.last>30 or wall<=self.last_wall or wall-self.last_wall>30
                or abs((wall-self.last_wall)-(stamp-self.last))>5):
            self.identity=identity;self.first=stamp
        self.last=stamp;self.last_wall=wall
        elapsed=stamp-self.first
        return {'trigger':elapsed>=300,'state':'ABOVE','high_sample':True,
                'limit_bytes':self.limit,'continuous_seconds':elapsed}

def apply(row,decision,guard,now,request,runtime_started,context=None):
    # Match the actual current-tag child PID/start/boot and native monotonic
    # restart receipt. Wall-clock changes never shorten the300s suppression.
    boot=False
    if runtime_started:
        stamp=row.get('mono')
        if (not isinstance(request,dict) or not isinstance(context,dict)
            or type(request.get('authority')) is not int or request.get('authority')!=830
            or type(request.get('attempt')) is not int or request.get('attempt')!=1
            or type(context.get('supervisor_pid')) is not int or context['supervisor_pid']<=0
            or type(context.get('supervisor_start')) is not int or context['supervisor_start']<=0
            or not isinstance(context.get('tag'),str) or not context['tag']
            or request.get('argv')!=['systemctl','--user','restart','clarp-runtime.service']
            or any(request.get(k)!=context.get(k) for k in
                   ('tag','supervisor_pid','supervisor_start','boot'))
            or row.get('boot')!=context.get('boot')
            or any(type(x) not in (int,float) or not math.isfinite(x)
                   for x in (stamp,now,request.get('monotonic'),context.get('launch_mono'),context.get('launch_epoch')))
            or request['monotonic']<context['launch_mono'] or request['monotonic']>stamp
            or abs((now-context['launch_epoch'])-(stamp-context['launch_mono']))>5):
            boot=None
        else:boot=stamp-request['monotonic']<300
    episode=guard.sample(row,row.get('mono'),boot)
    # Change ONLY the former single-sample RSS decision, retain allothercriteria.
    result={'triggers':[x for x in decision['triggers'] if x!='rss'],
            'unknown':list(decision['unknown'])}
    if episode['trigger']:result['triggers'].append('rss')
    if episode['state'] in ('UNKNOWN','UNKNOWN_BOOT_BINDING'):result['unknown'].append('rss_continuity')
    row['rss_r2_911']=episode
    return result
