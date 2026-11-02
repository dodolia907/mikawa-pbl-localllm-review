# review-pr と review-server から source して使う。色の設定と、対話用の入力・メニュー。
# 画面に出す文言は英語。

# 端末に出すときだけ色や記号を使う
if [ -t 2 ]; then
  DIM=$'\e[2m' BOLD=$'\e[1m' CYAN=$'\e[36m' GREEN=$'\e[32m' RED=$'\e[31m' RESET=$'\e[0m'
else
  DIM= BOLD= CYAN= GREEN= RED= RESET=
fi

# ask <変数名> <質問> [既定値]: 入力を変数に入れる。空欄なら既定値。
ask() {
  # 呼び出し側の変数名と重ならないよう、ローカル変数は _ask_ で始める
  local _ask_var=$1 _ask_q=$2 _ask_def=${3:-} _ask_in
  read -r -e -p "$_ask_q${_ask_def:+ [$_ask_def]}: " _ask_in < /dev/tty || { echo; echo "Cancelled." >&2; exit 1; }
  printf -v "$_ask_var" '%s' "${_ask_in:-$_ask_def}"
}

# menu <変数名> <見出し> <既定値> <"値<TAB>表示">...: 矢印キーか j/k で選ぶ。
#   ↑/k: 上、↓/j: 下、g/G: 先頭/末尾、1〜9: その番号へ、Enter: 決定、q: 中止
menu() {
  local _mn_var=$1 _mn_title=$2 _mn_def=$3; shift 3
  local -a _mn_val=() _mn_lab=()
  local _mn_o _mn_i _mn_sel=0 _mn_n _mn_key _mn_rest
  for _mn_o in "$@"; do _mn_val+=("${_mn_o%%$'\t'*}"); _mn_lab+=("${_mn_o#*$'\t'}"); done
  _mn_n=${#_mn_val[@]}
  for _mn_i in "${!_mn_val[@]}"; do
    if [ "${_mn_val[_mn_i]}" = "$_mn_def" ]; then _mn_sel=$_mn_i; fi
  done
  {
    printf '%s  \e[2m(↑↓ / j k to move, Enter to select, q to cancel)\e[0m\n' "$_mn_title"
    # カーソルを隠し、行の折り返しを止める(長い行が折り返すと描き直しがずれるため。はみ出た分は見えない)
    printf '\e[?25l\e[?7l'
    while :; do
      for _mn_i in "${!_mn_lab[@]}"; do
        if (( _mn_i == _mn_sel )); then printf '\e[7m> %s\e[0m\e[K\n' "${_mn_lab[_mn_i]}"
        else printf '  %s\e[K\n' "${_mn_lab[_mn_i]}"; fi
      done
      IFS= read -rsn1 _mn_key < /dev/tty || _mn_key=q
      if [[ $_mn_key == $'\e' ]]; then        # 矢印キーは ESC [ A のような 3 文字で届く
        IFS= read -rsn2 -t 0.1 _mn_rest < /dev/tty || true
        _mn_key+=$_mn_rest
      fi
      case $_mn_key in
        $'\e[A'|k) _mn_sel=$(( (_mn_sel + _mn_n - 1) % _mn_n )) ;;
        $'\e[B'|j) _mn_sel=$(( (_mn_sel + 1) % _mn_n )) ;;
        g) _mn_sel=0 ;;
        G) _mn_sel=$(( _mn_n - 1 )) ;;
        [1-9]) if (( _mn_key <= _mn_n )); then _mn_sel=$(( _mn_key - 1 )); fi ;;
        '') break ;;
        q) printf '\e[?25h\e[?7hCancelled.\n'; exit 1 ;;
      esac
      printf '\e[%dA' "$_mn_n"            # 選択肢の先頭に戻って描き直す
    done
    # メニューを消して、選んだものだけを 1 行で残す
    printf '\e[%dA\e[J\e[?25h\e[?7h' $(( _mn_n + 1 ))
    printf '%s: \e[1m%s\e[0m\n' "$_mn_title" "${_mn_lab[_mn_sel]}"
  } > /dev/tty
  printf -v "$_mn_var" '%s' "${_mn_val[_mn_sel]}"
}
