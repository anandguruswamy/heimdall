use std::{env, fs, path::PathBuf};

fn main() {
    let manifest = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let dist = manifest.join("../../dashboard/dist");
    println!("cargo:rerun-if-changed={}", dist.display());
    let mut files = Vec::new();
    if dist.is_dir() {
        collect(&dist, &dist, &mut files);
    }
    if files.is_empty() {
        files.push((
            "index.html".to_owned(),
            b"<!doctype html><title>Heimdall</title><main>Dashboard assets are not built.</main>"
                .to_vec(),
        ));
    }
    files.sort_by(|a, b| a.0.cmp(&b.0));
    let mut generated = String::from("pub static ASSETS: &[(&str, &[u8])] = &[\n");
    for (name, bytes) in files {
        generated.push_str(&format!("({name:?}, &{bytes:?}),\n"));
    }
    generated.push_str("];\n");
    fs::write(
        PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("assets.rs"),
        generated,
    )
    .unwrap();
}

fn collect(root: &std::path::Path, dir: &std::path::Path, out: &mut Vec<(String, Vec<u8>)>) {
    for entry in fs::read_dir(dir).unwrap().flatten() {
        let path = entry.path();
        if path.is_dir() {
            collect(root, &path, out);
        } else if path.is_file() {
            let name = path
                .strip_prefix(root)
                .unwrap()
                .to_string_lossy()
                .replace('\\', "/");
            out.push((name, fs::read(path).unwrap()));
        }
    }
}
