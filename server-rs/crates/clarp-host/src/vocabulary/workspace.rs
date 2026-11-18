//! Read names only after the assigned profile opts in. A non-git directory
//! contributes at most 3000 entries; directory links are never descended.
use std::path::Path;
use std::process::{Command,Stdio};
use clarp_kernel::vocabulary::sources::{identifiers,Sources,SKIP};

fn git(cwd:&Path,args:&[&str])->String{
    let output=Command::new("timeout").args(["3s","git","--no-pager"]).args(args).current_dir(cwd)
        .env("GIT_NO_LAZY_FETCH","1").env("GIT_TERMINAL_PROMPT","0").env("GIT_OPTIONAL_LOCKS","0")
        .stdin(Stdio::null()).stderr(Stdio::null()).output();
    output.ok().filter(|o|o.status.success()).map_or_else(String::new,|o|String::from_utf8_lossy(&o.stdout).into_owned())
}

pub(super) fn fill(s:&mut Sources,cwd:&Path){
    if cwd.as_os_str().is_empty() || !cwd.is_dir(){return;}
    s.project_name=cwd.file_name().unwrap_or_default().to_string_lossy().into();
    let mut paths=Vec::new();
    if !git(cwd,&["rev-parse","HEAD"]).trim().is_empty(){
        let top=git(cwd,&["rev-parse","--show-toplevel"]);
        if let Some(name)=Path::new(top.trim()).file_name(){s.project_name=name.to_string_lossy().into();}
        paths=git(cwd,&["ls-files","-z"]).split('\0').take(3000).map(str::to_string).collect();
        for name in git(cwd,&["branch","-a","--format=%(refname:short)","--sort=-committerdate"]).lines().filter(|s|!s.trim().is_empty()).take(30).filter(|s|!s.contains("HEAD")){
            let name=name.trim().trim_start_matches(['*',' ']).replace("remotes/origin/","");
            if !s.branches.contains(&name){s.branches.push(name);}
        }
        s.commit_subjects=git(cwd,&["log","-30","--format=%s","--no-merges"]).lines().filter(|s|!s.trim().is_empty()).map(str::to_string).collect();
    }else{
        let mut dirs=vec![cwd.to_path_buf()];let mut scanned=0;
        while let Some(dir)=dirs.pop(){
            let Ok(entries)=dir.read_dir()else{continue};
            for e in entries.flatten(){
                if scanned>=3000{break;}
                scanned+=1;
                let path=e.path();
                let Ok(rel)=path.strip_prefix(cwd)else{continue};
                let n=rel.to_string_lossy();
                let Ok(kind)=e.file_type()else{continue};
                if kind.is_file(){paths.push(n.into_owned());}
                else if kind.is_dir() && !SKIP.iter().any(|s|format!("{n}/").contains(s)){dirs.push(path);}
            }
            if scanned>=3000{break;}
        }
    }
    s.identifiers=identifiers(&paths);
}
