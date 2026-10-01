fn main() {
    // The dark widget style, to sit on the reading theme's dark palette.
    let config = slint_build::CompilerConfiguration::new().with_style("fluent-dark".into());
    slint_build::compile_with_config("ui/switcher.slint", config).expect("switcher.slint compiles");
}
