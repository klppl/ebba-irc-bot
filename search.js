var is_disabled = false;
var breweryComplete = {
	addBeerFinal: function () {

		$("#beer-add-box .errors:last").hide();
		$("#beer-add-box .errors:last").html('');
		$(".add-loading").show();
		$(".add-beer-btn").hide();

		var args = $(".add-beer-form").serialize();

		$.ajax({
			url: "/beer/add",
			type: "POST",
			data: args,
			dataType: "json",
			error: function (html) {
				$(".add-loading").hide();
				$(".add-beer-btn").show();

				$("#beer-add-box .errors:last").html("<li>Oh no! We have an issue with adding your beer. Please try again.</li>");
				$("#beer-add-box .errors:last").show();

				$('html,body').animate({ scrollTop: $("body").offset().top }, 'slow');
			},
			success: function (html) {
				if (html.result == "success") {
					window.location.href = "beer/" + html.bid;
				}
				else {
					$("#beer-add-box .errors:last").html(html.msg);
					$("#beer-add-box .errors:last").show();
					$(".add-loading").hide();
					$(".add-beer-btn").show();
				}
			}
		});
	},
	onKeyPress: function (e) {
		if (is_disabled) { return; }
		// return will exit the function
		// and event will not be prevented
		switch (e.keyCode) {
			case 27: //KEY_ESC:
				$(".brewery-complete").hide();
				break;
			case 9: //KEY_TAB:
			case 13: //KEY_RETURN:
				break;
			case 38: //KEY_UP:
				break;
			case 40: //KEY_DOWN:
				break;
			default:
				return;
		}
		e.stopImmediatePropagation();
		e.preventDefault();
	},
	onKeyUp: function (e) {
		if (is_disabled) { return; }
		switch (e.keyCode) {
			case 38: //KEY_UP:
			case 40: //KEY_DOWN:
				return;
		}
		clearInterval(onChangeInterval);
		if (currentValue !== $(".brewery_auto:last").val()) {
			$("#beer-add-box .brewery p.brewery-load").addClass("load");

			onChangeInterval = setInterval(function () { breweryComplete.onValueChange(); }, 500);
		}
	},
	onValueChange: function () {
		currentValue = $(".brewery_auto:last").val();

		clearInterval(onChangeInterval);
		if (currentValue === '' || currentValue.length < 3) {
			$("#beer-add-box .brewery p.brewery-load").removeClass("load");
		} else {
			breweryComplete.getSuggestions(currentValue);
		}
	},
	getSuggestions: function (q) {

		$("#beer-add-box .brewery p.brewery-load").addClass("load");

		if (is_disabled) {
			return;
		}

		$("#beer-add-box .errors:last").hide();

		var args = "q=" + encodeURIComponent(q);

		$.ajax({
			url: "/apireqs/autobrewery",
			type: "GET",
			data: args,
			dataType: "json",
			error: function (html) {
				$("#beer-add-box .brewery p.brewery-load").removeClass("load");

				$("#beer-add-box .errors:last").html("<li>Oh no! We have an issue with the autocomplete. Please try again.</li>");
				$("#beer-add-box .errors:last").show();

				//$('html,body').animate({scrollTop: $("body").offset().top},'slow');
			},
			success: function (html) {
				$("#beer-add-box .brewery p.brewery-load").removeClass("load");
				//$(".auto-loading").hide();

				var source = $("#brewery-autocomplete-template").html();
				var template = Handlebars.compile(source);

				var htmlData = template(html.response);

				$("#beer-add-box .brewery-ac").html(htmlData);
				$("#beer-add-box .brewery-ac").show();
			}
		});

	},
	toggle_brewery_add: function () {
		is_disabled = true;
		$("#beer-add-box .brewery-ac").hide();
		$("#beer-add-box .add-brewery").show();
		$("#beer-add-box .brewery .brewery-auto-container").hide();
		$("#is_new_brewery").val(1);
		$("#brewery_id").val(0);
	},
	cancelAdd: function () {
		is_disabled = false;
		$(".brewery_auto").val('');
		$("#beer-add-box .brewery .brewery-auto-container").show();
		$("#beer-add-box .add-brewery").hide();
		$("#is_new_brewery").val(0);
		$("#brewery_id").val(0);
	},
	set_brewery: function (a) {
		var bid = $(a).attr("data-brewery-id");
		var beer_name = $(a).find("span.name").html();
		$("#is_new_brewery").val(0);
		$("#brewery_id").val(bid);
		$("#beer-add-box .brewery-ac").hide();
		$("#brewery_auto").val(beer_name);
	}
}

var resizeLightbox = function () {
	var width = $(window).width();
	var staticBox = $(".add-beer-lightbox").width();

	var newWidth = (parseInt(width) - parseInt(staticBox)) / 2;
	$(".add-beer-lightbox").css("left", newWidth + "px");

	var hOfW = $(window).height() / 2;
	var hOfE = ($(".add-beer-lightbox").height() / 2);
	var height = hOfW - hOfE;
	$(".add-beer-lightbox").css("top", height + "px");
}

$(window).on("resize", function () {
	resizeLightbox();
})

$(document).ready(function () {

	if ($(".sidebar").is(":visible")) {
		var sidebarHeight = $(".sidebar")[0].scrollHeight;
		$(".main").css("min-height", sidebarHeight + 108 + "px");
	}

	$(document).on("keydown", "#brewery_auto", function (e) { breweryComplete.onKeyPress(e); });
	$(document).on("keyup", "#brewery_auto", function (e) { breweryComplete.onKeyUp(e); });
	$(document).on("click", ".new-brewery", function (e) { breweryComplete.toggle_brewery_add(); });
	$(document).on("click", ".cancel-new-brewery", function (e) { breweryComplete.cancelAdd(); });
	$(document).on("click", ".brewery-ac a.select-brewery", function (e) { breweryComplete.set_brewery(this); return false; });
	$(document).on("click", "#facybox_overlay", function () { $(this).hide(); $(".add-beer-lightbox").hide(); });
	$(document).on("click", ".add-beer-btn", function () { breweryComplete.addBeerFinal(); });
	$(document).on("click", "#beer-add-box .close", function () { $("#facybox_overlay").hide(); $(".add-beer-lightbox").hide(); });

	$(".section-toggle").on("click", function () {
		var _section = $(this).attr('data-filter');
		$("#page-type").val(_section);
		$("#sort-type").val('');
		$(".search form").submit();
		return false;
	})

	$("#sort_picker").on("change", function () {
		var _sort = $(this).val();
		$("#sort-type").val(_sort);
		$(".search form").submit();
	})

	$(document).on("change", "#beer-add-box .select_input", function () {
		if ($(this).val() == "0") {
			$(this).addClass("empty");
		}
		else {
			$(this).removeClass("empty")
		}
	});

	$(".more_search").on("click", function () {
		var _this = $(this);
		$(_this).hide();
		var pageType = $(_this).attr('data-type');
		var search = $(".search form input[type='text']").val();
		var sort = $(_this).attr('data-sort');

		$(".stream-loading").addClass("active");

		var offset = $(".beer-item").length;

		$.ajax({
			url: "/search/more_search/" + pageType + "?offset=" + offset + "&q=" + encodeURIComponent(search) + "&sort=" + sort,
			type: "GET",
			error: function (xhr) {
				$(".stream-loading").removeClass("active");
				$(_this).show();
				$.notifyBar({
					html: "Hmm. Something went wrong. Please try again!",
					delay: 2000,
					animationSpeed: "normal"
				});
			},
			success: function (html) {
				$(".stream-loading").removeClass("active");

				if (html == "") {
					$(".more_beer").hide();
				}
				else {
					$(_this).show();
					$(".results-container").append(html);
				}
			}
		});

		return false;
	});
})