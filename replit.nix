{ pkgs }: {
  deps = [
    pkgs.python312
    pkgs.chromium
    pkgs.ffmpeg
    pkgs.aria2
    pkgs.megatools
    pkgs.cacert
  ];
  env = {
    CHROMIUM_EXECUTABLE_PATH = "${pkgs.chromium}/bin/chromium";
  };
}
