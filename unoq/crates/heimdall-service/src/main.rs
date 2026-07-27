use std::{
    fs::File,
    io::Read,
    net::SocketAddr,
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver, SyncSender, TrySendError},
    },
    thread,
    time::{Duration, Instant},
};

#[cfg(unix)]
use std::os::fd::AsRawFd;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use heimdall_service::{
    api::{self, AppState},
    archive::{ArchiveWriter, ordered_segments, verify_archive},
    metadata::Metadata,
    pipeline::{Pipeline, PipelineSummary},
};
use serde_json::json;

#[derive(Parser)]
#[command(
    name = "heimdall-service",
    version,
    about = "Heimdall UNO Q fusion service"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Serve {
        #[arg(
            long,
            default_value = "/dev/serial/by-id/usb-Open_UWB_Heimdall_Gateway_7556160612A31510-if00"
        )]
        device: PathBuf,
        #[arg(long, default_value = "data")]
        data: PathBuf,
        #[arg(long, default_value = "0.0.0.0:8080")]
        bind: SocketAddr,
    },
    Replay {
        path: PathBuf,
        #[arg(long, default_value_t = 65536)]
        chunk_size: usize,
    },
    Inspect {
        path: PathBuf,
    },
    Verify {
        archive: PathBuf,
        #[arg(long)]
        database: PathBuf,
    },
    Benchmark {
        #[arg(long, default_value_t = 8)]
        nodes: usize,
        #[arg(long, default_value_t = 100_000)]
        iterations: usize,
    },
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();
    match Cli::parse().command {
        Command::Serve { device, data, bind } => serve(device, data, bind).await,
        Command::Replay { path, chunk_size } => print_summary(replay(&path, chunk_size)?),
        Command::Inspect { path } => inspect(&path),
        Command::Verify { archive, database } => verify(&archive, &database),
        Command::Benchmark { nodes, iterations } => benchmark(nodes, iterations),
    }
}

async fn serve(device: PathBuf, data: PathBuf, bind: SocketAddr) -> Result<()> {
    std::fs::create_dir_all(&data)?;
    let metadata = Metadata::open(data.join("heimdall.sqlite3"))?;
    let run_id = metadata.start_run()?;
    let state = AppState::new(metadata.clone(), &data)?;
    let listener = tokio::net::TcpListener::bind(bind).await?;
    let running = Arc::new(AtomicBool::new(true));
    let (processing_tx, processing_rx) = mpsc::sync_channel(128);
    let processor = spawn_processor(processing_rx, state.clone());
    let reader = spawn_cdc(
        device,
        data.join("raw"),
        metadata.clone(),
        run_id,
        state.clone(),
        running.clone(),
        processing_tx,
    );
    tracing::info!(%bind, "service listening");
    let result = api::serve(listener, state).await;
    running.store(false, Ordering::Release);
    reader
        .join()
        .map_err(|_| anyhow::anyhow!("CDC worker panicked"))?;
    processor
        .join()
        .map_err(|_| anyhow::anyhow!("processing worker panicked"))?;
    metadata.stop_run(run_id)?;
    result
}

fn spawn_cdc(
    device: PathBuf,
    archive_root: PathBuf,
    metadata: Arc<Metadata>,
    run_id: i64,
    state: AppState,
    running: Arc<AtomicBool>,
    processing_tx: SyncSender<ProcessingInput>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        while running.load(Ordering::Acquire) {
            match File::open(&device) {
                Ok(mut input) => {
                    if let Err(error) = configure_raw_tty(&input) {
                        tracing::warn!(%error, device=%device.display(), "failed to configure raw CDC mode");
                        thread::sleep(Duration::from_secs(1));
                        continue;
                    }
                    let connection_id =
                        match metadata.open_connection(run_id, &device.to_string_lossy()) {
                            Ok(id) => id,
                            Err(error) => {
                                tracing::error!(%error, "connection metadata failed");
                                thread::sleep(Duration::from_secs(1));
                                continue;
                            }
                        };
                    let mut archive = match ArchiveWriter::new(
                        &archive_root,
                        metadata.clone(),
                        Some(connection_id),
                    ) {
                        Ok(archive) => archive,
                        Err(error) => {
                            state.archive_errors.fetch_add(1, Ordering::Relaxed);
                            state.archive_paused.store(true, Ordering::Release);
                            *state.archive_last_error.lock() = Some(error.to_string());
                            tracing::error!(%error, "archive open failed");
                            let _ = metadata.close_connection(connection_id, "archive-error");
                            thread::sleep(Duration::from_secs(1));
                            continue;
                        }
                    };
                    let mut buffer = [0_u8; 64 * 1024];
                    let mut processing_buffer = Vec::with_capacity(16 * 1024);
                    let mut last_processing_flush = Instant::now();
                    state.clips.reset_stream();
                    let _ = processing_tx.send(ProcessingInput::Reset);
                    while running.load(Ordering::Acquire) {
                        match input.read(&mut buffer) {
                            Ok(0) => break,
                            Ok(count) => {
                                if let Err(error) = archive.write(&buffer[..count]) {
                                    state.archive_errors.fetch_add(1, Ordering::Relaxed);
                                    state.archive_paused.store(true, Ordering::Release);
                                    *state.archive_last_error.lock() = Some(error.to_string());
                                    tracing::error!(%error, "archive write failed; parser not fed");
                                    break;
                                }
                                if state.archive_paused.swap(false, Ordering::AcqRel) {
                                    *state.archive_last_error.lock() = None;
                                }
                                state
                                    .clips
                                    .ingest(heimdall_service::metadata::now_ns(), &buffer[..count]);
                                processing_buffer.extend_from_slice(&buffer[..count]);
                                if processing_buffer.len() >= 16 * 1024
                                    || last_processing_flush.elapsed() >= Duration::from_millis(5)
                                {
                                    let batch = std::mem::replace(
                                        &mut processing_buffer,
                                        Vec::with_capacity(16 * 1024),
                                    );
                                    if let Err(error) =
                                        processing_tx.try_send(ProcessingInput::Data(batch))
                                        && matches!(error, TrySendError::Full(_))
                                    {
                                        state.processing_drops.fetch_add(1, Ordering::Relaxed);
                                    }
                                    last_processing_flush = Instant::now();
                                }
                            }
                            Err(error) => {
                                tracing::warn!(%error, "CDC read failed");
                                break;
                            }
                        }
                    }
                    if !processing_buffer.is_empty()
                        && let Err(error) =
                            processing_tx.try_send(ProcessingInput::Data(processing_buffer))
                        && matches!(error, TrySendError::Full(_))
                    {
                        state.processing_drops.fetch_add(1, Ordering::Relaxed);
                    }
                    if let Err(error) = archive.finish() {
                        state.archive_errors.fetch_add(1, Ordering::Relaxed);
                        state.archive_paused.store(true, Ordering::Release);
                        *state.archive_last_error.lock() = Some(error.to_string());
                        tracing::error!(%error, "archive finalization failed");
                    }
                    let _ = metadata.close_connection(connection_id, "disconnected");
                }
                Err(error) => {
                    tracing::warn!(%error, device=%device.display(), "CDC open failed; reconnecting")
                }
            }
            if running.load(Ordering::Acquire) {
                thread::sleep(Duration::from_secs(1));
            }
        }
    })
}

enum ProcessingInput {
    Reset,
    Data(Vec<u8>),
}

fn spawn_processor(receiver: Receiver<ProcessingInput>, state: AppState) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        while let Ok(input) = receiver.recv() {
            match input {
                ProcessingInput::Reset => state.pipeline.lock().reset_connection(),
                ProcessingInput::Data(bytes) => {
                    let messages = state
                        .pipeline
                        .lock()
                        .feed_with_topics(&bytes, state.topic_mask());
                    for message in messages {
                        let _ = state.stream.send(message);
                    }
                }
            }
        }
    })
}

#[cfg(unix)]
fn configure_raw_tty(file: &File) -> Result<()> {
    let fd = file.as_raw_fd();
    let mut settings = std::mem::MaybeUninit::<libc::termios>::uninit();
    // SAFETY: tcgetattr initializes the termios structure for a valid open fd.
    if unsafe { libc::tcgetattr(fd, settings.as_mut_ptr()) } != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    // SAFETY: settings was initialized by successful tcgetattr.
    let mut settings = unsafe { settings.assume_init() };
    // SAFETY: cfmakeraw and tcsetattr operate on the initialized local structure.
    unsafe {
        libc::cfmakeraw(&mut settings);
        if libc::tcsetattr(fd, libc::TCSANOW, &settings) != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
    }
    Ok(())
}

#[cfg(not(unix))]
fn configure_raw_tty(_file: &File) -> Result<()> {
    Ok(())
}

fn replay(path: &Path, chunk_size: usize) -> Result<PipelineSummary> {
    let mut pipeline = Pipeline::new();
    let paths = if path.is_dir() {
        ordered_segments(path)?
    } else {
        vec![path.to_path_buf()]
    };
    let mut buffer = vec![0; chunk_size.max(1)];
    for path in paths {
        let mut file = File::open(&path).with_context(|| format!("open {}", path.display()))?;
        loop {
            let count = file.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            pipeline.feed(&buffer[..count]);
        }
    }
    Ok(pipeline.summary())
}

fn inspect(path: &Path) -> Result<()> {
    let summary = replay(path, 65536)?;
    println!("{}", serde_json::to_string_pretty(&summary)?);
    Ok(())
}

fn verify(archive: &Path, database: &Path) -> Result<()> {
    let metadata = Metadata::open(database)?;
    let rows = verify_archive(archive, &metadata)?;
    let summary = replay(archive, 65536)?;
    let parser = &summary.parser;
    if summary.rejected != 0
        || parser.buffered_bytes != 0
        || parser.crc_failures != 0
        || parser.framing_errors != 0
        || parser.unsupported_versions != 0
        || parser.unknown_types != 0
    {
        anyhow::bail!(
            "protocol verification failed: {}",
            serde_json::to_string(&summary)?
        );
    }
    println!(
        "{}",
        serde_json::to_string_pretty(&json!({
            "sqlite": metadata.integrity_check()?, "segments": rows.len(), "pipeline": summary
        }))?
    );
    Ok(())
}

fn benchmark(nodes: usize, iterations: usize) -> Result<()> {
    if !(2..=8).contains(&nodes) {
        anyhow::bail!("nodes must be between 2 and 8");
    }
    let links = nodes * (nodes - 1);
    let started = Instant::now();
    let mut checksum = 0_u64;
    for round in 0..iterations {
        for from in 0..nodes {
            for to in 0..nodes {
                if from == to {
                    continue;
                }
                let cfo = ((round + from * 17 + to * 31) as f64 * 0.001).sin();
                let cir = (0..64)
                    .map(|tap| (-(tap as f64 - 20.0 - cfo).powi(2) / 8.0).exp())
                    .collect::<Vec<_>>();
                let spectrum = heimdall_dsp::fast_fft(
                    &cir,
                    64.0,
                    heimdall_dsp::FftWindow::Hann,
                    heimdall_dsp::QualityFlags::NONE,
                );
                checksum = checksum.wrapping_add(spectrum.bins[1].norm().to_bits());
            }
        }
    }
    let elapsed = started.elapsed();
    println!(
        "{}",
        serde_json::to_string_pretty(&json!({
            "nodes": nodes, "links": links, "iterations": iterations, "link_updates": links * iterations,
            "elapsed_seconds": elapsed.as_secs_f64(), "updates_per_second": links as f64 * iterations as f64 / elapsed.as_secs_f64(),
            "checksum": checksum
        }))?
    );
    Ok(())
}

fn print_summary(summary: PipelineSummary) -> Result<()> {
    println!("{}", serde_json::to_string_pretty(&summary)?);
    Ok(())
}
