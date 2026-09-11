"""Targeted owner-requested portrait change, preserving unrelated learned code."""
from __future__ import annotations


def avatar_edits(program: dict, helper: str) -> dict:
    source=program['files']['world.js']
    if 'drawAgentAvatar' in source:
        return {'change':{'kind':'unchanged','evidence':'Agent portraits already replace boats','preserved':'World and learned behavior'},'edits':[]}
    start=source.index('function vessel(')
    end=source.index('module.exports.render=',start)
    marker=source[start:end]
    args='interaction={},reducedMotion=false})=>'
    call='vessel(c,p.x,p.y,ev.agent||id,col,reducedMotion?0:ambient%1,active);'
    for text in [args,call]:
        if source.count(text)!=1:raise ValueError('Live source changed; inspect before adapting the portrait marker')
    return {'change':{'kind':'repair','evidence':'Owner explicitly requested agent avatars instead of boats','preserved':'World identity, all layouts, routes, activity machinery and learned behaviors'},
        'edits':[
            {'file':'world.js','before':marker,'after':"const {drawAgentAvatar}=require('./avatar.js');\n"},
            {'file':'world.js','before':args,'after':'interaction={},reducedMotion=false,avatars={}})=>'},
            {'file':'world.js','before':call,'after':'drawAgentAvatar(c,p.x,p.y,ev.agent||id,col,reducedMotion?0:ambient%1,active,avatars[id]);'},
        ],'new_files':{'avatar.js':helper},'notes':'Replace boat markers with existing agent portraits; preserve the rest of the world.'}
