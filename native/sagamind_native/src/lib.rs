//! SagaMind native acceleration kernels (§4.5).
//!
//! Optional PyO3 extension providing a vectorised pairwise cosine-distance matrix for
//! memory consolidation. `src/memory/consolidation.py` imports this module and falls
//! back to NumPy (or pure Python) when it is not built/installed, so this crate is a
//! pure performance optimisation — never a hard dependency.

use pyo3::prelude::*;

/// Compute an (n, n) pairwise cosine-distance matrix for a set of equal-length vectors.
///
/// Returns `1.0` for any pair involving a zero-norm vector, matching
/// `MemoryConsolidator.compute_cosine_distance`'s convention.
#[pyfunction]
fn cosine_distance_matrix(embeddings: Vec<Vec<f64>>) -> PyResult<Vec<Vec<f64>>> {
    let n = embeddings.len();
    let norms: Vec<f64> = embeddings
        .iter()
        .map(|v| v.iter().map(|x| x * x).sum::<f64>().sqrt())
        .collect();

    let mut dist = vec![vec![0.0f64; n]; n];
    for i in 0..n {
        for j in (i + 1)..n {
            let d = if norms[i] == 0.0 || norms[j] == 0.0 {
                1.0
            } else {
                let dot: f64 = embeddings[i]
                    .iter()
                    .zip(embeddings[j].iter())
                    .map(|(a, b)| a * b)
                    .sum();
                1.0 - (dot / (norms[i] * norms[j]))
            };
            dist[i][j] = d;
            dist[j][i] = d;
        }
    }
    Ok(dist)
}

#[pymodule]
fn sagamind_native(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(cosine_distance_matrix, m)?)?;
    Ok(())
}
