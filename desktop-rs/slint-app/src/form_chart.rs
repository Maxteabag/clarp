//! A `clarp.form` v2 chart (issue 673) as the form card draws it: the
//! marks as SVG path data in a fixed view box the card stretches to the
//! plot's size, the axis ticks as fractions of it, the dots of a short line
//! and the legend. Slint draws no chart, so this is all the geometry; the
//! card only places it. The values are not interactive.
//!
//! A plot's view box is [`PLOT_W`] by [`PLOT_H`] (y down); a pie's is
//! [`PIE`] square. Bar and pie take every x as a category in the order
//! given; only a line with numeric x draws them to scale. y always includes
//! zero, as the iOS app's charts do.

use std::f64::consts::{FRAC_PI_2, PI, TAU};

use clarp_core::render_spec::{Chart, ChartKind, X, number_text};

pub const PLOT_W: f64 = 1000.0;
pub const PLOT_H: f64 = 200.0;
pub const PIE: f64 = 200.0;
/// Colours run in series order (a pie's in point order); the card's palette has this many.
pub const PALETTE: i32 = 12;
/// A line with at most this many points marks each one.
const DOTS_UP_TO: usize = 60;

/// One shape: a line (stroked) or bars or a slice (filled), in a palette colour.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Mark {
    pub commands: String,
    pub color: i32,
    pub fill: bool,
}

/// An axis label at a fraction of the plot (x from the left, y from the top).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Tick {
    pub at: f32,
    pub text: String,
}

/// A line's point, as fractions of the plot.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Dot {
    pub x: f32,
    pub y: f32,
    pub color: i32,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct Key {
    pub label: String,
    pub color: i32,
}

/// The chart as the card draws it.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Drawn {
    /// bar, line or pie.
    pub kind: &'static str,
    pub title: String,
    pub marks: Vec<Mark>,
    pub dots: Vec<Dot>,
    pub x_ticks: Vec<Tick>,
    pub y_ticks: Vec<Tick>,
    pub keys: Vec<Key>,
    pub x_label: String,
    pub y_label: String,
    /// What a screen reader says for the whole chart.
    pub summary: String,
}

fn n(value: f64) -> String {
    let rounded = (value * 10.0).round() / 10.0;
    if rounded == rounded.trunc() { format!("{}", rounded as i64) } else { format!("{rounded:.1}") }
}

/// A step of 1, 2 or 5 times a power of ten that cuts `range` into about
/// `parts`, never below one (the values are integers).
fn nice_step(range: f64, parts: f64) -> f64 {
    let raw = (range / parts).max(f64::MIN_POSITIVE);
    let magnitude = 10f64.powf(raw.log10().floor());
    let factor = match raw / magnitude {
        f if f <= 1.0 => 1.0,
        f if f <= 2.0 => 2.0,
        f if f <= 5.0 => 5.0,
        _ => 10.0,
    };
    (factor * magnitude).max(1.0)
}

/// The y axis: its low and high ends on whole steps, zero included.
struct Scale {
    low: f64,
    high: f64,
    step: f64,
}

impl Scale {
    fn of(values: impl Iterator<Item = i64>) -> Scale {
        let (mut low, mut high) = (0f64, 0f64);
        for value in values {
            low = low.min(value as f64);
            high = high.max(value as f64);
        }
        if high <= low {
            high = low + 1.0;
        }
        let step = nice_step(high - low, 4.0);
        Scale { low: (low / step).floor() * step, high: (high / step).ceil() * step, step }
    }

    /// From the top, 0 to 1.
    fn fraction(&self, value: f64) -> f64 {
        (self.high - value) / (self.high - self.low)
    }

    fn ticks(&self) -> Vec<f64> {
        let count = ((self.high - self.low) / self.step).round() as i64;
        (0..=count).map(|i| self.low + i as f64 * self.step).collect()
    }
}

pub fn draw(chart: &Chart, separator: char) -> Drawn {
    let mut drawn = Drawn {
        kind: match chart.kind {
            ChartKind::Bar => "bar",
            ChartKind::Line => "line",
            ChartKind::Pie => "pie",
        },
        title: chart.title.clone().unwrap_or_default(),
        x_label: chart.x_label.clone().unwrap_or_default(),
        y_label: chart.y_label.clone().unwrap_or_default(),
        summary: chart.summary(separator),
        ..Drawn::default()
    };
    if chart.kind == ChartKind::Pie {
        pie(chart, separator, &mut drawn);
        return drawn;
    }
    let scale = Scale::of(chart.series.iter().flat_map(|s| s.points.iter().map(|p| p.y)));
    let y_at = |value: i64| scale.fraction(value as f64) * PLOT_H;
    drawn.y_ticks = scale.ticks().into_iter().map(|v| Tick { at: scale.fraction(v) as f32, text: chart.value_text(v.round() as i64, separator) }).collect();
    if chart.legend() {
        drawn.keys = chart.series.iter().enumerate().map(|(i, s)| Key { label: s.name.clone(), color: i as i32 % PALETTE }).collect();
    }
    let categories = chart.categories();
    let band = PLOT_W / categories.len().max(1) as f64;
    let slot = |x: &X| categories.iter().position(|c| c == x).unwrap_or(0) as f64;

    if chart.kind == ChartKind::Bar {
        let count = chart.series.len().max(1) as f64;
        let width = band * 0.8 / count;
        let zero = y_at(0);
        for (index, series) in chart.series.iter().enumerate() {
            let mut commands = String::new();
            for point in &series.points {
                let left = slot(&point.x) * band + band * 0.1 + index as f64 * width;
                let right = left + width * 0.9;
                let (top, bottom) = { let y = y_at(point.y); (y.min(zero), y.max(zero)) };
                commands.push_str(&format!("M {} {} H {} V {} H {} Z ", n(left), n(top), n(right), n(bottom), n(left)));
            }
            drawn.marks.push(Mark { commands: commands.trim_end().to_owned(), color: index as i32 % PALETTE, fill: true });
        }
        drawn.x_ticks = category_ticks(&categories, band);
        return drawn;
    }

    // A line: categories in their bands, or numeric x to scale.
    let scaled = chart.scaled_x();
    let (low, high) = if scaled {
        let numbers = chart.series.iter().flat_map(|s| s.points.iter()).filter_map(|p| if let X::Number(x) = p.x { Some(x) } else { None });
        numbers.fold((i64::MAX, i64::MIN), |(lo, hi), x| (lo.min(x), hi.max(x)))
    } else {
        (0, 0)
    };
    let x_at = |x: &X| -> f64 {
        match x {
            X::Number(value) if scaled => {
                if high > low { 20.0 + (*value - low) as f64 / (high - low) as f64 * (PLOT_W - 40.0) } else { PLOT_W / 2.0 }
            }
            other => (slot(other) + 0.5) * band,
        }
    };
    for (index, series) in chart.series.iter().enumerate() {
        let color = index as i32 % PALETTE;
        let mut commands = String::new();
        for (offset, point) in series.points.iter().enumerate() {
            let (x, y) = (x_at(&point.x), y_at(point.y));
            commands.push_str(&format!("{} {} {} ", if offset == 0 { "M" } else { "L" }, n(x), n(y)));
            if series.points.len() <= DOTS_UP_TO {
                drawn.dots.push(Dot { x: (x / PLOT_W) as f32, y: (y / PLOT_H) as f32, color });
            }
        }
        if series.points.len() > 1 {
            drawn.marks.push(Mark { commands: commands.trim_end().to_owned(), color, fill: false });
        }
    }
    drawn.x_ticks = if scaled {
        if high > low {
            let step = nice_step((high - low) as f64, 6.0);
            let first = ((low as f64) / step).ceil() * step;
            let mut ticks = Vec::new();
            let mut value = first;
            while value <= high as f64 {
                ticks.push(Tick { at: (x_at(&X::Number(value as i64)) / PLOT_W) as f32, text: number_text(value as i64, 0, separator) });
                value += step;
            }
            ticks
        } else {
            vec![Tick { at: 0.5, text: number_text(low, 0, separator) }]
        }
    } else {
        category_ticks(&categories, band)
    };
    drawn
}

/// Every category's label up to eight; past that six spread from the first
/// to the last, so labels never pile up.
fn category_ticks(categories: &[X], band: f64) -> Vec<Tick> {
    let count = categories.len();
    let picked: Vec<usize> = if count <= 8 {
        (0..count).collect()
    } else {
        let mut picked: Vec<usize> = (0..6).map(|j| ((j * (count - 1)) as f64 / 5.0).round() as usize).collect();
        picked.dedup();
        picked
    };
    picked.into_iter().map(|i| Tick { at: (((i as f64) + 0.5) * band / PLOT_W) as f32, text: categories[i].words() }).collect()
}

/// A pie: one slice per point from the top, clockwise; the legend names
/// each point with its value.
fn pie(chart: &Chart, separator: char, drawn: &mut Drawn) {
    let Some(series) = chart.series.first() else { return };
    let total: f64 = series.points.iter().map(|p| p.y as f64).sum();
    drawn.keys = series.points.iter().enumerate().map(|(i, p)| Key { label: format!("{} · {}", p.x.words(), chart.value_text(p.y, separator)), color: i as i32 % PALETTE }).collect();
    if total <= 0.0 {
        return;
    }
    let (center, radius) = (PIE / 2.0, PIE / 2.0 - 4.0);
    let at = |angle: f64| (center + radius * angle.cos(), center + radius * angle.sin());
    let mut start = -FRAC_PI_2;
    for (index, point) in series.points.iter().enumerate() {
        if point.y <= 0 {
            continue;
        }
        let sweep = point.y as f64 / total * TAU;
        let color = index as i32 % PALETTE;
        let commands = if sweep >= TAU - 1e-9 {
            // The whole pie: two half circles (one arc cannot end where it starts).
            let (l, r) = (center - radius, center + radius);
            format!("M {} {c} A {radius} {radius} 0 1 1 {} {c} A {radius} {radius} 0 1 1 {} {c} Z", n(l), n(r), n(l), c = n(center), radius = n(radius))
        } else {
            let (x0, y0) = at(start);
            let (x1, y1) = at(start + sweep);
            let large = if sweep > PI { 1 } else { 0 };
            format!("M {c} {c} L {} {} A {r} {r} 0 {large} 1 {} {} Z", n(x0), n(y0), n(x1), n(y1), c = n(center), r = n(radius))
        };
        drawn.marks.push(Mark { commands, color, fill: true });
        start += sweep;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use clarp_core::render_spec::{Point, Series};

    fn chart(kind: ChartKind, series: Vec<(&str, Vec<(X, i64)>)>) -> Chart {
        Chart {
            kind,
            title: Some("T".into()),
            x_label: None,
            y_label: None,
            unit: None,
            decimals: 0,
            series: series.into_iter().map(|(name, points)| Series { name: name.into(), points: points.into_iter().map(|(x, y)| Point { x, y }).collect() }).collect(),
        }
    }

    fn cat(text: &str) -> X {
        X::Category(text.into())
    }

    #[test]
    fn the_y_axis_runs_on_whole_steps_from_zero() {
        let mut c = chart(ChartKind::Bar, vec![("a", vec![(cat("Mon"), 120), (cat("Tue"), 77)])]);
        c.decimals = 1;
        c.unit = Some("ms".into());
        let drawn = draw(&c, ',');
        let labels: Vec<&str> = drawn.y_ticks.iter().map(|t| t.text.as_str()).collect();
        assert_eq!(labels, ["0,0 ms", "5,0 ms", "10,0 ms", "15,0 ms"], "125 with decimals 1 reads 12,5; the axis in the same digits");
        assert_eq!(drawn.y_ticks.first().map(|t| t.at), Some(1.0), "zero at the bottom");
        assert_eq!(drawn.y_ticks.last().map(|t| t.at), Some(0.0));
        assert!(drawn.keys.is_empty(), "one series has no legend");
    }

    #[test]
    fn bars_sit_in_their_category_side_by_side() {
        let c = chart(ChartKind::Bar, vec![("a", vec![(cat("x"), 10), (cat("y"), 5)]), ("b", vec![(cat("y"), 10)])]);
        let drawn = draw(&c, '.');
        assert_eq!(drawn.marks.len(), 2);
        assert!(drawn.marks.iter().all(|m| m.fill));
        assert_eq!(drawn.marks[0].commands.matches('M').count(), 2, "one bar per point");
        assert_eq!(drawn.marks[1].commands, "M 750 0 H 930 V 200 H 750 Z", "b's bar in y's band, after a's");
        assert_eq!(drawn.keys.iter().map(|k| (k.label.as_str(), k.color)).collect::<Vec<_>>(), [("a", 0), ("b", 1)]);
        assert_eq!(drawn.x_ticks.iter().map(|t| t.text.as_str()).collect::<Vec<_>>(), ["x", "y"]);
    }

    #[test]
    fn a_numeric_line_draws_x_to_scale_and_bar_takes_numbers_as_categories() {
        let points = vec![(X::Number(1), 0), (X::Number(2), 5), (X::Number(10), 10)];
        let line = draw(&chart(ChartKind::Line, vec![("a", points.clone())]), '.');
        assert_eq!(line.marks[0].commands, "M 20 200 L 126.7 100 L 980 0");
        assert_eq!(line.dots.len(), 3);
        let ticks: Vec<&str> = line.x_ticks.iter().map(|t| t.text.as_str()).collect();
        assert_eq!(ticks, ["2", "4", "6", "8", "10"]);
        let bar = draw(&chart(ChartKind::Bar, vec![("a", points)]), '.');
        assert_eq!(bar.x_ticks.iter().map(|t| t.text.as_str()).collect::<Vec<_>>(), ["1", "2", "10"], "in the order given, evenly spaced");
    }

    #[test]
    fn many_categories_label_six() {
        let points: Vec<(X, i64)> = (0..60).map(|i| (cat(&format!("d{i}")), i)).collect();
        let drawn = draw(&chart(ChartKind::Bar, vec![("a", points)]), '.');
        assert_eq!(drawn.x_ticks.iter().map(|t| t.text.as_str()).collect::<Vec<_>>(), ["d0", "d12", "d24", "d35", "d47", "d59"]);
    }

    #[test]
    fn a_pie_slices_from_the_top_and_names_each_point() {
        let drawn = draw(&chart(ChartKind::Pie, vec![("share", vec![(cat("a"), 1), (cat("b"), 3), (cat("c"), 0)])]), ',');
        assert_eq!(drawn.marks.len(), 2, "a zero slice is not drawn");
        assert_eq!(drawn.marks[0].commands, "M 100 100 L 100 4 A 96 96 0 0 1 196 100 Z", "a quarter from the top, clockwise");
        assert!(drawn.marks[1].commands.contains(" 0 1 1 "), "three quarters take the large arc");
        assert_eq!(drawn.keys.iter().map(|k| k.label.as_str()).collect::<Vec<_>>(), ["a · 1", "b · 3", "c · 0"]);
        let whole = draw(&chart(ChartKind::Pie, vec![("share", vec![(cat("a"), 5)])]), ',');
        assert_eq!(whole.marks[0].commands, "M 4 100 A 96 96 0 1 1 196 100 A 96 96 0 1 1 4 100 Z");
        assert!(draw(&chart(ChartKind::Pie, vec![("share", vec![(cat("a"), 0)])]), ',').marks.is_empty());
    }
}
