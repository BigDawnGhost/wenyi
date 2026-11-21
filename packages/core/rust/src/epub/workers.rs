//! A bounded, ordered resource pool shared by native EPUB operations.

use std::sync::{LazyLock, Mutex, MutexGuard};

use rayon::prelude::*;

static ADMISSION: Mutex<()> = Mutex::new(());

pub(crate) fn admit() -> MutexGuard<'static, ()> {
    ADMISSION
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

static POOL: LazyLock<Result<rayon::ThreadPool, rayon::ThreadPoolBuildError>> =
    LazyLock::new(|| {
        rayon::ThreadPoolBuilder::new()
            .num_threads(
                std::thread::available_parallelism()
                    .map_or(1, |n| n.get())
                    .min(6),
            )
            .thread_name(|n| format!("wenyi-epub-{n}"))
            .build()
    });

pub(super) fn ordered_map<T: Send, U: Send>(
    items: Vec<T>,
    f: impl Fn(T) -> U + Sync + Send,
) -> Vec<U> {
    if items.len() >= 12 {
        if let Ok(pool) = &*POOL {
            return pool.install(|| items.into_par_iter().map(f).collect());
        }
    }
    items.into_iter().map(f).collect()
}
