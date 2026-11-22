//! Small native UI preferences stored under the selected data root.

use std::{
    io::{self, Read, Write},
    path::Path,
};

use cap_fs_ext::{DirExt, FollowSymlinks, OpenOptionsFollowExt};
use cap_std::fs::{Dir, OpenOptions};
use serde::{de::DeserializeOwned, Serialize};

use crate::publish::Temporary;

const MAX_BYTES: usize = 1024;

pub(crate) fn read<T: DeserializeOwned>(root: &Path, name: &str) -> io::Result<T> {
    let root = Dir::open_ambient_dir(root, cap_std::ambient_authority())?;
    let directory = root.open_dir_nofollow("runtime")?;
    if !directory.symlink_metadata(name)?.is_file() {
        return Err(io::Error::other("Invalid preference file"));
    }
    let mut options = OpenOptions::new();
    options.read(true).follow(FollowSymlinks::No);
    let file = directory.open_with(name, &options)?;
    if !file.metadata()?.is_file() {
        return Err(io::Error::other("Invalid preference file"));
    }
    let mut bytes = Vec::new();
    file.take((MAX_BYTES + 1) as u64).read_to_end(&mut bytes)?;
    if bytes.len() > MAX_BYTES {
        return Err(io::Error::other("Invalid preference size"));
    }
    Ok(serde_json::from_slice(&bytes)?)
}

pub(crate) fn save(root: &Path, name: &str, value: &impl Serialize) -> io::Result<()> {
    let root = Dir::open_ambient_dir(root, cap_std::ambient_authority())?;
    root.create_dir_all("runtime")?;
    let directory = root.open_dir_nofollow("runtime")?;
    let mut random = [0; 16];
    getrandom::fill(&mut random).map_err(|_| io::Error::other("Secure randomness unavailable"))?;
    let temporary = format!(".{name}-{:032x}.tmp", u128::from_ne_bytes(random));
    let mut options = OpenOptions::new();
    options
        .write(true)
        .create_new(true)
        .follow(FollowSymlinks::No);
    let mut file = directory.open_with(&temporary, &options)?;
    let temporary = Temporary {
        dir: &directory,
        name: temporary,
    };
    let write = (|| {
        file.write_all(&serde_json::to_vec(value)?)?;
        file.sync_all()
    })();
    drop(file);
    write?;
    directory.rename(&temporary.name, &directory, name)
}
