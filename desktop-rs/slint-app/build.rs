fn main() {
    // Fluent follows a colour scheme set at runtime from the reading theme.
    // Debug builds carry the element tree's debug info, which the checks
    // need to find the transcript's rows on screen (scroll_checks.rs).
    let debug = std::env::var("PROFILE").is_ok_and(|p| p == "debug");
    let config = slint_build::CompilerConfiguration::new().with_style("fluent".into()).with_debug_info(debug);
    slint_build::compile_with_config("ui/app.slint", config).expect("app.slint compiles");
}
