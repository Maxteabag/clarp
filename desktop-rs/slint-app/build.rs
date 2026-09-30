fn main() {
    // Fluent follows a colour scheme set at runtime from the reading theme.
    let config = slint_build::CompilerConfiguration::new().with_style("fluent".into());
    slint_build::compile_with_config("ui/app.slint", config).expect("app.slint compiles");
}
